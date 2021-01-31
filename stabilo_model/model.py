import math
from typing import Optional, Tuple, Any
import torch
import torch.nn as nn
import torch.nn.functional as F
from .utils import clones

'''
    Transformer adapted for time series according to the paper "A TRANSFORMER-BASED FRAMEWORK FOR MULTIVARIATE
    TIME SERIES REPRESENTATION LEARNING" by G. Zerveas et al.  
'''

class ScaledDotProductAttention(Module):
    def __init__(self, d_k:int): self.d_k = d_k
    def forward(self, q:Tensor, k:Tensor, v:Tensor, mask:Optional[Tensor]=None):

        # MatMul (q, k) - similarity scores for all pairs of positions in an input sequence
        scores = torch.matmul(q, k)                                         # scores : [bs x n_heads x q_len x q_len]
        
        # Scale
        scores = scores / (self.d_k ** 0.5)
        
        # Mask (optional)
        if mask is not None: scores.masked_fill_(mask, -1e9)
        
        # SoftMax
        attn = F.softmax(scores, dim=-1)                                    # attn   : [bs x n_heads x q_len x q_len]
        
        # MatMul (attn, v)
        context = torch.matmul(attn, v)                                     # context: [bs x n_heads x q_len x d_v]
        
        return context, attn

class MultiHeadAttention(Module):
    def __init__(self, d_model:int, n_heads:int, d_k:int, d_v:int):
        """
        Input shape:  Q, K, V:[batch_size (bs) x q_len x d_model], mask:[q_len x q_len]
        """
        self.n_heads, self.d_k, self.d_v = n_heads, d_k, d_v
        
        self.W_Q = nn.Linear(d_model, d_k * n_heads, bias=False)
        self.W_K = nn.Linear(d_model, d_k * n_heads, bias=False)
        self.W_V = nn.Linear(d_model, d_v * n_heads, bias=False)
        
        self.W_O = nn.Linear(n_heads * d_v, d_model, bias=False)

    def forward(self, Q:Tensor, K:Tensor, V:Tensor, mask:Optional[Tensor]=None):
        
        bs = Q.size(0)

        # Linear (+ split in multiple heads)
        q_s = self.W_Q(Q).view(bs, -1, self.n_heads, self.d_k).transpose(1,2)       # q_s    : [bs x n_heads x q_len x d_k]
        k_s = self.W_K(K).view(bs, -1, self.n_heads, self.d_k).permute(0,2,3,1)     # k_s    : [bs x n_heads x d_k x q_len] - transpose(1,2) + transpose(2,3)
        v_s = self.W_V(V).view(bs, -1, self.n_heads, self.d_v).transpose(1,2)       # v_s    : [bs x n_heads x q_len x d_v]

        # Scaled Dot-Product Attention (multiple heads)
        context, attn = ScaledDotProductAttention(self.d_k)(q_s, k_s, v_s)          # context: [bs x n_heads x q_len x d_v], attn: [bs x n_heads x q_len x q_len]

        # Concat
        context = context.transpose(1, 2).contiguous().view(bs, -1, self.n_heads * self.d_v) # context: [bs x q_len x n_heads * d_v]

        # Linear
        output = self.W_O(context)                                                  # context: [bs x q_len x d_model]
        
        return output, attn

class EncoderLayer(Module):
    def __init__(self, d_model:int, n_heads:int, d_k:Optional[int]=None, d_v:Optional[int]=None, d_ff:int=256, res_dropout:float=0.1, activation:str="gelu"):

        assert d_model // n_heads, f"d_model ({d_model}) must be divisible by n_heads ({n_heads})"
        d_k = ifnone(d_k, d_model // n_heads)
        d_v = ifnone(d_v, d_model // n_heads)

        # Multi-Head attention
        self.self_attn = MultiHeadAttention(d_model, n_heads, d_k, d_v)

        # Add & Norm
        self.dropout_attn = nn.Dropout(res_dropout)
        self.batchnorm_attn = nn.BatchNorm1d(d_model)

        # Position-wise Feed-Forward
        self.ff = nn.Sequential(nn.Linear(d_model, d_ff), self._get_activation_fn(activation), nn.Linear(d_ff, d_model))

        # Add & Norm
        self.dropout_ffn = nn.Dropout(res_dropout)
        self.batchnorm_ffn = nn.BatchNorm1d(d_model)

    def forward(self, src:Tensor, mask:Optional[Tensor]=None) -> Tensor:

        # Multi-Head attention sublayer
        ## Multi-Head attention
        src2, attn = self.self_attn(src, src, src, mask=mask)
        ## Add & Norm
        src = src + self.dropout_attn(src2) # Add: residual connection with residual dropout
        src = self.batchnorm_attn(src.permute(1,2,0)).permute(2,0,1) # Norm: batchnorm (requires d_model features to be in dim 1)

        # Feed-forward sublayer
        ## Position-wise Feed-Forward
        src2 = self.ff(src)
        ## Add & Norm
        src = src + self.dropout_ffn(src2) # Add: residual connection with residual dropout
        src = self.batchnorm_ffn(src.permute(1,2,0)).permute(2,0,1) # Norm: batchnorm (requires d_model features to be in dim 1)

        return src

    def _get_activation_fn(self, activation):
        if activation == "relu": return nn.ReLU()
        elif activation == "gelu": return nn.GELU()
        else: return activation()

class Encoder(Module):
    def __init__(self, encoder_layer, n_layers):
        self.layers = nn.ModuleList([deepcopy(encoder_layer) for i in range(n_layers)])

    def forward(self, src:Tensor, mask:Optional[Tensor]=None) -> Tensor:
        output = src
        for mod in self.layers: output = mod(output, mask=mask)
        return output

class TransformerEncoder(Module):
    def __init__(self, c_in:int, c_out:int, seq_len:int, max_seq_len:Optional[int]=None, 
                 n_layers:int=3, d_model:int=128, n_heads:int=16, d_k:Optional[int]=None, d_v:Optional[int]=None,  
                 d_ff:int=256, res_dropout:float=0.1, act:str="gelu", fc_dropout:float=0., 
                 y_range:Optional[tuple]=None, verbose:bool=False, **kwargs):

        self.c_out, self.seq_len = c_out, seq_len
        
        # Input encoding
        q_len = seq_len
        self.new_q_len = False
        if max_seq_len is not None and seq_len > max_seq_len: # Control temporal resolution
            self.new_q_len = True
            q_len = max_seq_len
            tr_factor = math.ceil(seq_len / q_len)
            total_padding = (tr_factor * q_len - seq_len)
            padding = (total_padding // 2, total_padding - total_padding // 2)
            self.W_P = nn.Sequential(Pad1d(padding), Conv1d(c_in, d_model, kernel_size=tr_factor, stride=tr_factor))
            pv(f'temporal resolution modified: {seq_len} --> {q_len} time steps: kernel_size={tr_factor}, stride={tr_factor}, padding={padding}.\n', verbose)
        elif kwargs:
            self.new_q_len = True
            t = torch.rand(1, 1, seq_len)
            q_len = nn.Conv1d(1, 1, **kwargs)(t).shape[-1]
            self.W_P = nn.Conv1d(c_in, d_model, **kwargs) # Eq 2
            pv(f'Conv1d with kwargs={kwargs} applied to input to create input encodings\n', verbose)
        else:
            self.W_P = nn.Linear(c_in, d_model) # Eq 1: projection of feature vectors onto a d-dim vector space

        # Positional encoding
        W_pos = torch.zeros((q_len, d_model), device=default_device())
        self.W_pos = nn.Parameter(W_pos, requires_grad=True)
       
        # Residual dropout
        self.res_dropout = nn.Dropout(res_dropout)

        # Encoder
        encoder_layer = EncoderLayer(d_model, n_heads, d_k=d_k, d_v=d_v, d_ff=d_ff, res_dropout=res_dropout, activation=act)
        self.encoder = Encoder(encoder_layer, n_layers)
        self.flatten = Flatten()
        
        # Head
        self.head_nf = q_len * d_model
        self.head = self.create_head(self.head_nf, c_out, fc_dropout=fc_dropout, y_range=y_range)

    def create_head(self, nf, c_out, fc_dropout=0., y_range=None, **kwargs):
        layers = [nn.Dropout(fc_dropout)] if fc_dropout else []
        layers += [nn.Linear(nf, c_out)]
        if y_range: layers += [SigmoidRange(*y_range)]
        return nn.Sequential(*layers)    
        

    def forward(self, x:Tensor, mask:Tensor=gmask) -> Tensor:  # x: [bs x nvars x q_len]

        # Input encoding
        if self.new_q_len: u = self.W_P(x).transpose(2,1) # Eq 2        # u: [bs x d_model x q_len] transposed to [bs x q_len x d_model]
        else: u = self.W_P(x.transpose(2,1)) # Eq 1                     # u: [bs x q_len x d_model] transposed to [bs x q_len x d_model]

        # Positional encoding
        u = self.res_dropout(u + self.W_pos)

        # Encoder
        z = self.encoder(u)                                             # z: [bs x q_len x d_model]
        if self.flatten is not None: z = self.flatten(z)                # z: [bs x q_len * d_model]
        else: z = z.transpose(2,1).contiguous()                         # z: [bs x d_model x q_len]

        # Classification/ Regression head
        return self.head(z)                                             # output: [bs x c_out]