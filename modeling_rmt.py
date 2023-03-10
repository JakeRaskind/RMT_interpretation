import numpy as np
import torch
import torch.nn.functional as F
from typing import List, Optional, Tuple, Union
from transformers import PreTrainedModel, AutoModelForSequenceClassification

import math

class RMTEncoderForSequenceClassification():
    def __init__(self, config=None, base_model=None, **kwargs):
        if config is not None:
            self.model = AutoModelForSequenceClassification(config, **kwargs)
        
        if base_model is not None:
            self.model = base_model


    def from_pretrained(from_pretrained, **kwargs):
        base_model = AutoModelForSequenceClassification.from_pretrained(from_pretrained, **kwargs)
        rmt = RMTEncoderForSequenceClassification(base_model=base_model)
        return rmt
        

    def set_params(self, 
                model_attr='bert', 
                drop_empty_segments=True,
                sum_loss=False,
                input_size=None, 
                input_seg_size=None, 
                backbone_cls=None,
                num_mem_tokens=0, 
                bptt_depth=-1, 
                pad_token_id=0, 
                eos_token_id=1,
                cls_token_id=101, 
                sep_token_id=102):
        print('Setting Parameters')
        self.net = getattr(self.model, model_attr)
        if input_size is not None:
            self.input_size = input_size
        else:
            self.input_size =  self.net.embeddings.position_embeddings.weight.shape[0]
        self.input_seg_size = input_seg_size

        self.bptt_depth = bptt_depth
        self.pad_token_id = pad_token_id
        self.cls_token = torch.tensor([cls_token_id])
        self.sep_token = torch.tensor([sep_token_id])
        self.num_mem_tokens = num_mem_tokens
        self.drop_empty_segments = drop_empty_segments
        self.sum_loss = sum_loss
        self.extend_word_embeddings()


    def set_memory(self, memory=None):
        if memory is None:
            mem_token_ids = self.mem_token_ids.to(device=self.device)
            memory = self.net.embeddings.word_embeddings(mem_token_ids)
        return memory
    
    def extend_word_embeddings(self):
        vocab_size = self.net.embeddings.word_embeddings.weight.shape[0]
        extended_vocab_size = vocab_size + self.num_mem_tokens
        self.mem_token_ids = torch.arange(vocab_size, vocab_size + self.num_mem_tokens)
        self.net.resize_token_embeddings(extended_vocab_size)


    def __call__(self, input_ids, **kwargs):
        memory = self.set_memory()
        segmented = self.pad_and_segment(input_ids)

        outputs = []
        for seg_num, segment_data in enumerate(zip(*segmented)):
            input_ids, attention_mask, token_type_ids = segment_data
            if memory.ndim == 2:
                memory = memory.repeat(input_ids.shape[0], 1, 1)
            if (self.bptt_depth > -1) and (len(segmented) - seg_num > self.bptt_depth): 
                memory = memory.detach()

            seg_kwargs = dict(**kwargs)
            if self.drop_empty_segments:

                non_empty_mask = [not torch.equal(input_ids[i], self.empty) for i in range(len(input_ids))]
                if sum(non_empty_mask) == 0:
                    continue
                input_ids = input_ids[non_empty_mask]
                attention_mask = attention_mask[non_empty_mask]
                token_type_ids = token_type_ids[non_empty_mask]
                seg_kwargs['labels'] = seg_kwargs['labels'][non_empty_mask]

                inputs_embeds = self.net.embeddings.word_embeddings(input_ids)
                inputs_embeds[:, 1:1+self.num_mem_tokens] = memory[non_empty_mask]
            else:
                inputs_embeds = self.net.embeddings.word_embeddings(input_ids)
                inputs_embeds[:, 1:1+self.num_mem_tokens] = memory

            seg_kwargs['inputs_embeds'] = inputs_embeds
            seg_kwargs['attention_mask'] = attention_mask
            seg_kwargs['token_type_ids'] = token_type_ids
            
            out = self.model.forward(**seg_kwargs, output_hidden_states=True)
            outputs.append(out)

            if self.drop_empty_segments:
                memory[non_empty_mask] = out.hidden_states[-1][:, :self.num_mem_tokens]
            else:
                memory = out.hidden_states[-1][:, :self.num_mem_tokens]

        if self.sum_loss:
            out['loss'] = torch.stack([o['loss'] for o in outputs]).sum(dim=-1)
        return outputs

    def pad_and_segment(self, input_ids):
        
        sequence_len = input_ids.shape[1]
        input_seg_size = self.input_size - self.num_mem_tokens - 3 
        if self.input_seg_size is not None and self.input_seg_size < input_seg_size:
            input_seg_size = self.input_seg_size
            
        n_segments = math.ceil(sequence_len / input_seg_size)

        augmented_inputs = []
        for input in input_ids:
            input = input[input != self.pad_token_id][1:-1]

            seg_sep_inds = [0] + list(range(len(input), 0, -input_seg_size))[::-1] # chunk so that first segment has various size
            input_segments = [input[s:e] for s, e in zip(seg_sep_inds, seg_sep_inds[1:])]

            def pad_add_special_tokens(tensor, seg_size):
                tensor = torch.cat([self.cls_token.to(device=self.device),
                                    self.mem_token_ids.to(device=self.device),
                                    self.sep_token.to(device=self.device),
                                    tensor.to(device=self.device),
                                    self.sep_token.to(device=self.device)])
                pad_size = seg_size - tensor.shape[0]
                if pad_size > 0:
                    tensor = F.pad(tensor, (0, pad_size))
                return tensor

            input_segments = [pad_add_special_tokens(t, self.input_size) for t in input_segments]
            empty = torch.Tensor([]).int()
            self.empty = pad_add_special_tokens(empty, self.input_size)
            empty_segments = [self.empty for i in range(n_segments - len(input_segments))]
            input_segments = empty_segments + input_segments

            augmented_input = torch.cat(input_segments)
            augmented_inputs.append(augmented_input)
            
        augmented_inputs = torch.stack(augmented_inputs)
        attention_mask = torch.ones_like(augmented_inputs)
        attention_mask[augmented_inputs == self.pad_token_id] = 0

        token_type_ids = torch.zeros_like(attention_mask)

        input_segments = torch.chunk(augmented_inputs, n_segments, dim=1)
        attention_mask = torch.chunk(attention_mask, n_segments, dim=1)
        token_type_ids = torch.chunk(token_type_ids, n_segments, dim=1)
    
        return input_segments, attention_mask, token_type_ids


    def to(self, device):
        self.model = self.model.to(device)
        
    
    def cuda(self):
        self.model.cuda()


    def __getattr__(self, attribute):
        return getattr(self.model, attribute)


    def parameters(self, **kwargs):
        return self.model.parameters(**kwargs)

    def named_parameters(self, **kwargs):
        return self.model.named_parameters(**kwargs)
