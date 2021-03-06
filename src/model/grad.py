import json
from datasets import load_dataset, load_metric, load_from_disk
import pandas as pd
from transformers import T5Model, T5ForConditionalGeneration, T5Tokenizer
from transformers import Adafactor
import torch
from torch import nn
import numpy as np
import torch.nn.functional as F
from os.path import dirname, abspath
from dataclasses import dataclass, field
from collections import namedtuple
from typing import List
from utils import *
import math
#from torchviz import make_dot

@dataclass
class Options:  # class for storing hyperparameters and other options

    max_length : int = 384  # use interim data if changed 
    batch_size : int = 4
    embed_dim : int = 768  # typical base model embedding dimension
    pretrained_model_name : str = 't5-base'
    act_vocab_size : int = 32100  # get from tokenizer
    num_epochs : int = 3

    # adafactor hyperparameters
    lr : float = 1e-5
    eps: tuple = (1e-30, 1e-3)
    clip_threshold : float = 1.0
    decay_rate : float = -0.8
    beta1 : float = None
    weight_decay : float = 0.0
    relative_step : bool = False
    scale_parameter : bool = False
    warmup_init : bool = False

    # gumbel softmax
    tau : float = 1.0

    # directories
    root : str = field(init=False)
    pretrained_model : str = field(init=False)
    qr_finetuned : str = field(init=False)
    rc_finetuned : str = field(init=False)
    tokenizer : str = field(init=False)

    # dataset
    processed_dataset_dir : str = field(init=False)
    processed_dataset_format : List = field(default_factory = lambda: ['ctx_input_ids', 'rwrt_input_ids', 'psg_input_ids',
            'ans_input_ids', 'ctx_attention_mask', 'rwrt_attention_mask', 'psg_attention_mask'])

    # add methods to init dataclass attributes here
    def __post_init__(self):

        self.root = self.get_root_dir()
        self.pretrained_model = self.root + '/models/pretrained_models/t5-base'
        self.qr_finetuned = self.root + '/models/finetuned_weights/qr_gen4.pth'
        self.rc_finetuned = self.root + '/models/finetuned_weights/rc_gen2.pth'
        self.tokenizer = self.root + '/models/pretrained_models/t5-tokenizer'

        self.processed_dataset_dir = self.root +'/data/processed/dataset/'
        

    def get_root_dir(self):
        root = abspath(__file__)
        while root.split('/')[-1] != 'conv-qa':
            root = dirname(root)
        return root

         

class End2End(nn.Module):

    def __init__(self, options):  
        super().__init__()        

        # load T5 models
        self.qr_model = T5ForConditionalGeneration.from_pretrained(options.pretrained_model)
        self.rc_model = T5ForConditionalGeneration.from_pretrained(options.pretrained_model)



    def load_weights(self, device):

        # load finetuned weights
        self.qr_model.load_state_dict(torch.load(options.qr_finetuned, map_location=device))
        self.rc_model.load_state_dict(torch.load(options.rc_finetuned, map_location=device))  


    def save_models(self, options, epoch):

        torch.save(self.qr_model.state_dict(), options.root+'/models/finetuned_weights/e2e_ff_qr'+str(epoch)+'.pth')
        torch.save(self.rc_model.state_dict(), options.root+'/models/finetuned_weights/e2e_ff_rc'+str(epoch)+'.pth')


    
    def forward(self, batch, options, device):

        # context + question input
        ctx_input = batch['ctx_input_ids'].to(device)  # QR input
        ctx_attention = batch['ctx_attention_mask'].to(device)

        # gold rewrite input for qr loss
        rwrt_input = batch['rwrt_input_ids']
        # tokens with indices set to -100 are ignored (masked)
        rwrt_input[rwrt_input == tokenizer.pad_token_id] = -100
        rwrt_input = rwrt_input.to(device)
        rwrt_attention = batch['rwrt_attention_mask'].to(device) # b, 384

        # passage input
        psg_input = batch['psg_input_ids'].to(device)
        # need to add sep token at the begining
        # roll by 1 and add column of 1s
        psg_input = torch.roll(psg_input, 1, 1)
        psg_input[:, 0] = 1

        # answer input
        ans_input = batch['ans_input_ids']
        # # tokens with indices set to -100 are ignored (masked)
        ans_input[ans_input == tokenizer.pad_token_id] = -100
        ans_input = ans_input.to(device)

        # feed context+question input and rewrite label to qr model
        qr_output = self.qr_model(input_ids=ctx_input, attention_mask=ctx_attention, labels=rwrt_input, output_hidden_states=True)


        # logits to be sampled from
        logits = qr_output.logits
        #logits.retain_grad()

        # qr loss
        qr_loss = qr_output.loss

        # gumbel softmax on the logits
        # slice upto actual vocabulary size
        gumbel_output = F.gumbel_softmax(logits, tau=options.tau, hard=True)[..., :options.act_vocab_size]

        # print(gumbel_output.shape) # b, 384, 32100
        dummy = torch.arange(32100)
        gumbel_long = gumbel_output.long().cpu()
        outputs = gumbel_long@dummy
        outputs = torch.mul(outputs,(rwrt_attention.cpu()))
        print(tokenizer.batch_decode(outputs, skip_special_tokens=True))
        ###quit()


        # T5 input embeddings
        word_embeddings = self.rc_model.get_input_embeddings().weight[:options.act_vocab_size, :]  # 32100, 768

        # embedding of <pad> token we need for masking. assuming the first embedding corresponds to the pad token
        pad_embedding = word_embeddings[0] 

        #
 
        batch_list = []
        dummy = torch.ones(options.act_vocab_size).to(device)

        for i in range(gumbel_output.shape[0]):

            embedding_list = []

            for j in range(options.max_length):       
                ind = gumbel_output[i][j].expand(options.embed_dim, -1).T
                #ind = gumbel_output[i][j].repeat(options.embed_dim, 1).T
                embedding_list.append(torch.mul(ind, word_embeddings).T @ dummy)

            batch_list.append(torch.stack(embedding_list, dim=0))

        inputs_embeds = torch.stack(batch_list, dim=0)  
 
        # concat embeddings for max_length positions for batch
        #inputs_embeds = torch.cat(embedding_list, dim=1)

    
        # cast rewrite attention mask to float
        rwrt_attention_f = rwrt_attention.float()  # b, 384
    
        # mask rc input (inputs_embeds) with attention mask
        # masked positions are replaced with 0.0 vectors
        mask = rwrt_attention_f.view(rwrt_attention_f.shape[0], -1, 1) @ (torch.ones(1, options.embed_dim)).to(device)  # reshape mask

        #print(inputs_embeds)
        inputs_embeds = torch.mul(inputs_embeds, mask)


        #print(inputs_embeds)
        #print(psg_input)
        #print(inputs_embeds.shape)
        #print(psg_input.shape)
        #inputs_embeds[inputs_embeds.sum(dim=2)==0] = pad_embedding

        # now we need to fit the passage embeddings after the rewrite embeddings
        # flip the original rewrite attention mask, replace 1s with 0s and vice versa
        # now the 1s represent the 'free space' in the rc_input tensor to fit the passages
        flipped_rwrt_mask = torch.fliplr(rwrt_attention)
        flipped_mask = flipped_rwrt_mask.clone()
        flipped_mask[flipped_rwrt_mask == 0] = 1
        flipped_mask[flipped_rwrt_mask == 1] = 0

        # mask passage to extract ids that can fit in the rc_input tensor
        extr_psg = torch.mul(flipped_mask, psg_input)
        # find the shifts for each row of extr_psg
        # this is equal to the number of 1s in each row of rwrt_attention
        # reshape to column vector as required by the custom gather function
        shifts = (rwrt_attention == 1).sum(dim=1).reshape(-1, 1)
        # roll each row by the amount occupied by rc_input in that row
        trunc_psg = roll_by_gather(extr_psg, 1, shifts, device)

        #print(trunc_psg)
        #print(trunc_psg.shape)
        # reshape and repeat values for changing into embeddings afterwards
        trunc_psg = trunc_psg.view(trunc_psg.shape[0], -1, 1)
        trunc_psg = trunc_psg.repeat(1, 1, options.embed_dim)
        # cast to float
        trunc_psg = trunc_psg.float()
    
        # need to keep front zeros, since they will be replaced by the rewrite embeddings
        # replace end zeros with pad embedding
        for i in range(trunc_psg.shape[0]):
            flag = False
            for j in range(options.max_length):
                idx = trunc_psg[i][j][0].long()
                if idx == 0 and flag == False: continue
                flag = True
                #print(trunc_psg[i][j].shape)
                #print(embeddings[idx].shape)
                trunc_psg[i][j] = word_embeddings[idx]
       
        #print(trunc_psg)
        #print(trunc_psg.shape) 
        #print(pad_embedding)

        # add inputs_embeds and masked passage embeddings
        inputs_embeds = torch.add(inputs_embeds, trunc_psg)
        inputs_embeds.retain_grad()
        #print(inputs_embeds.shape) # b, 384, 768
        tokens = []
        for i in range(20):
            #print(i)
            em = inputs_embeds[3][i]
            for j in range(32100):
                if torch.equal(word_embeddings[j], em): tokens.append(j)

        print(tokenizer.decode(tokens, skip_special_tokens=True))

        rc_loss = self.rc_model(inputs_embeds=inputs_embeds, labels=ans_input).loss

        rc_loss.backward()
        #print(inputs_embeds.grad)
        #print(inputs_embeds.grad.shape) # b, 384, 768
        rr = inputs_embeds[3,:20,:]
        #print(rr[0].shape)
        rr_grad = inputs_embeds.grad[3, :20, :]
       
        #print(rr) 
        rr = rr - 10000000*rr_grad
        #print(rr.shape)
        #print(rr)

        cos = nn.CosineSimilarity(dim=0)

        tokens = []
        for i in range(20):

            sim = 0
            idx = 0

            for j in range(32100):
                s = cos(rr[i], word_embeddings[j])
                if s > sim:
                    sim = s
                    idx = j

            tokens.append(idx)
                
        print(tokenizer.decode(tokens, skip_special_tokens=True))
        quit()

        return qr_loss, rc_loss



if __name__ == '__main__':

    device = torch.device('cpu')

    # hyperparameters and other options
    options = Options()

    # end to end model
    e2epipe = End2End(options)
    e2epipe.to(device) 
    e2epipe.load_weights(device)  # finetuned weights
    e2epipe.train()

    # tokenizer
    tokenizer = T5Tokenizer.from_pretrained(options.tokenizer)

    # optimizer
    optim = Adafactor(
            e2epipe.parameters(),
            lr = options.lr,
            eps = options.eps,
            clip_threshold = options.clip_threshold,
            decay_rate = options.decay_rate,
            beta1 = options.beta1,
            weight_decay = options.weight_decay,
            relative_step= options.relative_step,
            scale_parameter = options.scale_parameter,
            warmup_init = options.warmup_init)

    # dataset

    dataset = load_from_disk(options.processed_dataset_dir)
    dataset.set_format(type='torch', columns = options.processed_dataset_format,)

    # dataloaders
    train_loader = torch.utils.data.DataLoader(dataset['train'], batch_size=options.batch_size)
    test_loader = torch.utils.data.DataLoader(dataset['test'], batch_size=options.batch_size)

    print('Number of batches : {}'.format(len(train_loader)))

    print('Start training')

    # train loop
    for epoch in range(1, options.num_epochs + 1):

        idx = 1

        qr_epoch_loss = 0
        rc_epoch_loss = 0

        for batch in train_loader:

            qr_loss, rc_loss = e2epipe(batch, options, device)  

            qr_epoch_loss += qr_loss.item()
            rc_epoch_loss += rc_loss.item()

            total_loss = sum([qr_loss, rc_loss])

            if idx % 500 == 0:
                print('epoch {}, batch {}'.format(epoch, idx))
 
            idx += 1

            optim.zero_grad()
            total_loss.backward()

            #for name, param in e2epipe.qr_model.named_parameters():
                #if param.requires_grad: print(name, param.grad)

            optim.step()

            del qr_loss, rc_loss, total_loss

        print('Train loss : {}, {}'.format(qr_epoch_loss/len(train_loader), rc_epoch_loss/len(train_loader)))

        e2epipe.eval()

        # valid loop
        qr_valid_loss = 0
        rc_valid_loss = 0

        idx = 0

        for batch in test_loader:

            qr_loss, rc_loss = e2epipe(batch, options, device)
            qr_valid_loss += qr_loss.item()
            rc_valid_loss += rc_loss.item()

            idx += 1

            del qr_loss, rc_loss

        print('Valid loss : {}, {}'.format(qr_valid_loss/idx, rc_valid_loss/idx))

        print('\n')

        e2epipe.train()
        e2epipe.save_models(options, epoch)
        print('Model saved')



 





    


       
