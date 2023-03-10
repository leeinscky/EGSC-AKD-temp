import torch
import random
import numpy as np
import torch.nn.functional as F
from tqdm import tqdm, trange
from scipy.stats import spearmanr, kendalltau

from layers import AttentionModule, TensorNetworkModule, DiffPool, ConfusionAttentionModule
from utils import calculate_ranking_correlation, calculate_prec_at_k, gen_pairs
from trans_modules import CrossAttentionModule

from torch_geometric.nn import GCNConv, GINConv
from torch_geometric.data import DataLoader, Batch
from torch_geometric.utils import to_dense_batch, to_dense_adj, degree
from torch_geometric.datasets import GEDDataset
from torch_geometric.transforms import OneHotDegree
import dgl.function as fn

import pdb

from layers import SETensorNetworkModule, AttentionModule_fix 
from layers import SEAttentionModule, repeat_certain_graph

class EGSC_generator(torch.nn.Module):
    def __init__(self, args, number_of_labels):
        super(EGSC_generator, self).__init__()
        self.args = args
        self.number_labels = number_of_labels
        self.dim_aug_feats = 0
        self.scaler_dim = 1
        self.setup_layers()
        # self.node_embeddings = None

    def calculate_bottleneck_features(self):
        if self.args.histogram:
            self.feature_count = self.args.tensor_neurons + self.args.bins + self.dim_aug_feats
        else:
            self.feature_count = self.args.tensor_neurons * 1 + self.dim_aug_feats

    def setup_layers(self):
        self.calculate_bottleneck_features()
        if self.args.gnn_operator == 'gcn':
            self.convolution_1 = GCNConv(self.number_labels, self.args.filters_1)
            self.convolution_2 = GCNConv(self.args.filters_1 * 1, self.args.filters_2)
            self.convolution_3 = GCNConv(self.args.filters_2 * 1, self.args.filters_3)
        elif self.args.gnn_operator == 'gin':
            nn1 = torch.nn.Sequential(
                torch.nn.Linear(self.number_labels, self.args.filters_1), 
                torch.nn.ReLU(), 
                torch.nn.Linear(self.args.filters_1, self.args.filters_1),
                torch.nn.BatchNorm1d(self.args.filters_1))
            
            nn2 = torch.nn.Sequential(
                torch.nn.Linear(self.args.filters_1, self.args.filters_2), 
                torch.nn.ReLU(), 
                torch.nn.Linear(self.args.filters_2, self.args.filters_2),
                torch.nn.BatchNorm1d(self.args.filters_2))
            
            nn3 = torch.nn.Sequential(
                torch.nn.Linear(self.args.filters_2, self.args.filters_3), 
                torch.nn.ReLU(), 
                torch.nn.Linear(self.args.filters_3, self.args.filters_3),
                torch.nn.BatchNorm1d(self.args.filters_3))
            
            self.convolution_1 = GINConv(nn1, train_eps=True)
            self.convolution_2 = GINConv(nn2, train_eps=True)
            self.convolution_3 = GINConv(nn3, train_eps=True)
        else:
            raise NotImplementedError('Unknown GNN-Operator.')
        
        if self.args.diffpool: # diffpool = False
            self.attention = DiffPool(self.args)
        else: # ????????????
            self.attention = AttentionModule(self.args, self.args.filters_3)
            self.attention_level2 = AttentionModule(self.args, self.args.filters_2 * self.scaler_dim)
            self.attention_level1 = AttentionModule(self.args, self.args.filters_1 * self.scaler_dim)

    def convolutional_pass_level1(self, edge_index, features):
        features = self.convolution_1(features, edge_index)
        features = F.relu(features)
        features_1 = F.dropout(features, p=self.args.dropout, training=self.training)
        return features_1

    def convolutional_pass_level2(self, edge_index, features):
        features_2 = self.convolution_2(features, edge_index)
        features_2 = F.relu(features_2)
        features_2 = F.dropout(features_2, p=self.args.dropout, training=self.training)
        return features_2

    def convolutional_pass_level3(self, edge_index, features):
        features_out = self.convolution_3(features, edge_index)
        return features_out

    def forward(self, edge_index, features, batch):

        features_level1 = self.convolutional_pass_level1(edge_index, features)

        features_level2 = self.convolutional_pass_level2(edge_index, features_level1)

        abstract_features = self.convolutional_pass_level3(edge_index, features_level2)
           
        pooled_features = self.attention(abstract_features, batch) # 128 * 16 ?????????????????????????????????
 
        pooled_features_level2 = self.attention_level2(features_level2, batch) # 128 * 32 ???????????????????????????

        pooled_features_level1 = self.attention_level1(features_level1, batch) # 128 * 64 ???????????????????????????

        pooled_features_all = torch.cat((pooled_features,pooled_features_level2,pooled_features_level1),dim=1) # ????????????
        
        # self.node_embeddings = pooled_features_all
        # print(f'[origin] pooled_features_all.shape: {pooled_features_all.shape}') # torch.Size([560, 112]) or torch.Size([48, 112]) 560 or 48 ???batch_size 112???????????????
        return  pooled_features_all

class EGSC_fusion(torch.nn.Module):
    def __init__(self, args, number_of_labels):
        super(EGSC_fusion, self).__init__()
        self.args = args
        self.number_labels = number_of_labels
        self.dim_aug_feats = 0
        self.scaler_dim = 1
        self.setup_layers()
        # self.node_embeddings = None

    def setup_layers(self):
        self.filter_dim_all = self.args.filters_3 + self.args.filters_2 + self.args.filters_1 # filters_1=64, filters_2=32, filters_3=16  64+32+16=112
        self.score_attention = SEAttentionModule(self.args, self.filter_dim_all * 2) # self.filter_dim_all * 2 = 112 * 2 = 224
        self.feat_layer = torch.nn.Linear(self.filter_dim_all * 2, self.filter_dim_all)
        self.fully_connected_first = torch.nn.Linear(self.filter_dim_all, self.args.bottle_neck_neurons)
        
    def forward(self, pooled_features_1_all, pooled_features_2_all):
        scores = torch.cat((pooled_features_1_all,pooled_features_2_all),dim=1) # ????????????????????????????????????????????????
        # self.node_embeddings = scores
        # print('[origin] scores.shape: ', scores.shape) # torch.Size([560, 224])
        scores = self.feat_layer(self.score_attention(scores) + scores)  # ???????????????????????????????????????????????????????????????????????????????????????
        scores = F.relu(self.fully_connected_first(scores)) # ????????????????????????relu????????????, relu???????????????????????????????????????0???????????????
        return  scores 

class EGSC_fusion_classifier(torch.nn.Module):
    def __init__(self, args, number_of_labels):
        super(EGSC_fusion_classifier, self).__init__()
        self.args = args
        self.number_labels = number_of_labels
        self.dim_aug_feats = 0
        self.scaler_dim = 1
        self.setup_layers()

    def setup_layers(self):
        self.feat_layer = torch.nn.Linear(self.args.bottle_neck_neurons * 2, self.args.bottle_neck_neurons)
        self.scoring_layer = torch.nn.Linear(self.args.bottle_neck_neurons, 1)

    def forward(self, scores):
        scores = F.relu(self.feat_layer(scores))
        scores = torch.sigmoid(self.scoring_layer(scores)).view(-1) # dim of score: 128 * 0
        return  scores 

class EGSC_classifier(torch.nn.Module): # ????????????Figure3??????????????????regression head???The regression head is a MLP which projects the joint embedding into the desired similarity.
    def __init__(self, args, number_of_labels):
        super(EGSC_classifier, self).__init__()
        self.args = args
        self.number_labels = number_of_labels
        self.dim_aug_feats = 0
        self.scaler_dim = 1
        self.setup_layers()

    def setup_layers(self):
        self.scoring_layer = torch.nn.Linear(self.args.bottle_neck_neurons, 1) # bottle_neck_neurons ???????????????=16 The bottleneck in a neural network is just a layer with fewer neurons than the layer below or above it. Having such a layer encourages the network to compress feature representations (of salient features for the target variable) to best fit in the available space???A bottleneck layer is a layer that contains few nodes compared to the previous layers. It can be used to obtain a representation of the input with reduced dimensionality. An example of this is the use of autoencoders with bottleneck layers for nonlinear dimensionality reduction.

    def forward(self, scores):
        score = torch.sigmoid(self.scoring_layer(scores)).view(-1)
        return  score 


class EGSC_teacher(torch.nn.Module): # EGSC_teacher ????????????????????????????????????model_g???model_f?????????
    def __init__(self, args, number_of_labels):
        super(EGSC_teacher, self).__init__()
        self.args = args
        self.number_labels = number_of_labels
        self.dim_aug_feats = 0
        self.scaler_dim = 1
        self.setup_layers()

    def calculate_bottleneck_features(self):
        if self.args.histogram:
            self.feature_count = self.args.tensor_neurons + self.args.bins + self.dim_aug_feats
        else:
            self.feature_count = (self.args.filters_1 + self.args.filters_2 + self.args.filters_3 ) // 2

    def setup_layers(self):
        self.calculate_bottleneck_features()
        if self.args.gnn_operator_fix == 'gcn':
            self.convolution_1 = GCNConv(self.number_labels, self.args.filters_1)
            self.convolution_2 = GCNConv(self.args.filters_1, self.args.filters_2)
            self.convolution_3 = GCNConv(self.args.filters_2, self.args.filters_3)

        elif self.args.gnn_operator_fix == 'gin':
            nn1 = torch.nn.Sequential(
                torch.nn.Linear(self.number_labels, self.args.filters_1), 
                torch.nn.ReLU(), 
                torch.nn.Linear(self.args.filters_1, self.args.filters_1),
                torch.nn.BatchNorm1d(self.args.filters_1))
            
            nn2 = torch.nn.Sequential(
                torch.nn.Linear(self.args.filters_1, self.args.filters_2), 
                torch.nn.ReLU(), 
                torch.nn.Linear(self.args.filters_2, self.args.filters_2),
                torch.nn.BatchNorm1d(self.args.filters_2))
            
            nn3 = torch.nn.Sequential(
                torch.nn.Linear(self.args.filters_2, self.args.filters_3), 
                torch.nn.ReLU(), 
                torch.nn.Linear(self.args.filters_3, self.args.filters_3),
                torch.nn.BatchNorm1d(self.args.filters_3))
            
            self.convolution_1 = GINConv(nn1, train_eps=True)
            self.convolution_2 = GINConv(nn2, train_eps=True)
            self.convolution_3 = GINConv(nn3, train_eps=True)
        elif self.args.gnn_operator_fix == 'gat':
            self.convolution_1 = GATConv(self.number_labels, self.args.filters_1)
            self.convolution_2 = GATConv(self.args.filters_1, self.args.filters_2)
            self.convolution_3 = GATConv(self.args.filters_2, self.args.filters_3)
        elif self.args.gnn_operator_fix == 'sage':
            self.convolution_1 = SAGEConv(self.number_labels, self.args.filters_1)
            self.convolution_2 = SAGEConv(self.args.filters_1, self.args.filters_2)
            self.convolution_3 = SAGEConv(self.args.filters_2, self.args.filters_3)

        else:
            raise NotImplementedError('Unknown GNN-Operator.')
        
        if self.args.diffpool:
            self.attention = DiffPool(self.args)
        else:
            self.attention_level3 = AttentionModule_fix(self.args, self.args.filters_3)
            self.attention_level2 = AttentionModule_fix(self.args, self.args.filters_2 * self.scaler_dim)
            self.attention_level1 = AttentionModule_fix(self.args, self.args.filters_1 * self.scaler_dim)

        self.cross_attention_level2 = CrossAttentionModule(self.args, self.args.filters_2)
        self.cross_attention_level3 = CrossAttentionModule(self.args, self.args.filters_3)
        self.cross_attention_level4 = CrossAttentionModule(self.args, self.args.filters_4)

        self.tensor_network_level3 = SETensorNetworkModule(self.args,dim_size=self.args.filters_3 * self.scaler_dim)
        self.tensor_network_level2 = SETensorNetworkModule(self.args,dim_size=self.args.filters_2 * self.scaler_dim)
        self.tensor_network_level1 = SETensorNetworkModule(self.args,dim_size=self.args.filters_1 * self.scaler_dim)
        self.fully_connected_first = torch.nn.Linear(self.feature_count, self.args.bottle_neck_neurons)
        self.scoring_layer = torch.nn.Linear(self.args.bottle_neck_neurons, 1)
       
        self.score_attention = SEAttentionModule(self.args, self.feature_count)
        
    def convolutional_pass_level1(self, edge_index, features):
        features = self.convolution_1(features, edge_index)
        features = F.relu(features)
        features_1 = F.dropout(features, p=self.args.dropout, training=self.training)
        return features_1

    def convolutional_pass_level2(self, edge_index, features):
        features_2 = self.convolution_2(features, edge_index)
        features_2 = F.relu(features_2)
        features_2 = F.dropout(features_2, p=self.args.dropout, training=self.training)
        return features_2

    def convolutional_pass_level3(self, edge_index, features):
        features_3 = self.convolution_3(features, edge_index)
        features_3 = F.relu(features_3)
        features_3 = F.dropout(features_3, p=self.args.dropout, training=self.training)
        return features_3

    def convolutional_pass_level4(self, edge_index, features):
        features_out = self.convolution_4(features, edge_index)
        return features_out
        

    def forward(self, edge_index_1, features_1, batch_1, edge_index_2, features_2, batch_2):

        features_level1_1 = self.convolutional_pass_level1(edge_index_1, features_1)
        features_level1_2 = self.convolutional_pass_level1(edge_index_2, features_2)

        pooled_features_level1_1 = self.attention_level1(features_level1_1, batch_1) # 128 * 64
        pooled_features_level1_2 = self.attention_level1(features_level1_2, batch_2) # 128 * 64
        scores_level1 = self.tensor_network_level1(pooled_features_level1_1, pooled_features_level1_2) # ???????????????????????????Embedding Fusion Net

        features_level2_1 = self.convolutional_pass_level2(edge_index_1, features_level1_1)
        features_level2_2 = self.convolutional_pass_level2(edge_index_2, features_level1_2)
        pooled_features_level2_1 = self.attention_level2(features_level2_1, batch_1) # 128 * 32
        pooled_features_level2_2 = self.attention_level2(features_level2_2, batch_2) # 128 * 32
        scores_level2 = self.tensor_network_level2(pooled_features_level2_1, pooled_features_level2_2) # ?????????????????????2???Embedding Fusion Net

        features_level3_1 = self.convolutional_pass_level3(edge_index_1, features_level2_1)
        features_level3_2 = self.convolutional_pass_level3(edge_index_2, features_level2_2)
        pooled_features_level3_1 = self.attention_level3(features_level3_1, batch_1) # 128 * 16
        pooled_features_level3_2 = self.attention_level3(features_level3_2, batch_2) # 128 * 16
        scores_level3 = self.tensor_network_level3(pooled_features_level3_1, pooled_features_level3_2) # ?????????????????????3???Embedding Fusion Net

        scores = torch.cat((scores_level3, scores_level2, scores_level1), dim=1)
        # print(f'[origin] scores.shape: {scores.shape}') # torch.Size([128, 56]) or torch.Size([48, 56])
        scores = F.relu(self.fully_connected_first(self.score_attention(scores)*scores + scores))
        
        return  scores

class logits_D(torch.nn.Module):
    def __init__(self, n_class, n_hidden):
        super(logits_D, self).__init__()
        # print('[EGSC-KD/src/model_kd.py] ????????????logits_D?????????????????? ????????????n_class:', n_class, 'n_hidden:', n_hidden) # ????????????n_class: 16 n_hidden: 16
        self.n_class = n_class
        self.n_hidden = n_hidden
        self.lin = torch.nn.Linear(self.n_hidden, self.n_hidden) # torch.nn.Linear(in_features, out_features, bias=True) 
        self.relu = torch.nn.ReLU() # ReLU??????, ?????????: y = max(0, x), ??????????????????[0, +???), ?????????????????????
        self.sigmoid = torch.nn.Sigmoid() # torch.nn.Sigmoid ??????????????????????????????sigmoid????????????, ?????????: y = 1 / (1 + exp(-x)), ??????????????????[0, 1], ?????????????????????, ??????: 0.5 > 0.5, ????????????, ???????????????, ????????????????????????, ????????????loss, ??????: BCELoss, CrossEntropyLoss???
        self.lin2 = torch.nn.Linear(self.n_hidden, self.n_class+1, bias=False) # ???????????????the output of ????l is a ???? + 1 dimensional vector with the first ???? for label prediction and the last for Real/Fake (namely teacher/student) indicator.

    def forward(self, logits, temperature=1.0):
        #print('[EGSC-KD/src/model_kd.py] ????????????logits_D???forward?????? ????????????logits.shape:', logits.shape, 'logits:', logits) # torch.Size([128, 16])
        out = self.lin(logits / temperature)
        #print('[EGSC-KD/src/model_kd.py] ????????????logits_D???forward?????? ??????out = self.lin(logits / temperature)????????????out.shape:', out.shape)
        #print('[EGSC-KD/src/model_kd.py] ????????????logits_D???forward?????? ??????out = self.lin(logits / temperature)????????????out:', out)
        out = logits + out
        #print('[EGSC-KD/src/model_kd.py] ????????????logits_D???forward?????? ??????out = logits + out????????????out.shape:', out.shape)
        #print('[EGSC-KD/src/model_kd.py] ????????????logits_D???forward?????? ??????out = logits + out????????????out:', out)
        if self.n_class == 16:
            out = self.relu(out)
        # elif self.n_class == 1:
        #     out = self.sigmoid(out)
        #print('[EGSC-KD/src/model_kd.py] ????????????logits_D???forward?????? ??????out = self.relu(out)????????????out.shape:', out.shape)
        #print('[EGSC-KD/src/model_kd.py] ????????????logits_D???forward?????? ??????out = self.relu(out)????????????out:', out)
        dist = self.lin2(out)
        # print('[EGSC-KD/src/model_kd.py] ????????????logits_D???forward?????? ??????dist = self.lin2(out)??????forward???????????????dist.shape:', dist.shape) # torch.Size([128, 17])
        #print('[EGSC-KD/src/model_kd.py] ????????????logits_D???forward?????? ??????dist = self.lin2(out)??????forward???????????????dist:', dist)
        return dist

class local_emb_D(torch.nn.Module): # discriminator of local node embeddings ????????????????????????????????????
    def __init__(self, n_hidden):
        super(local_emb_D, self).__init__()
        self.n_hidden = n_hidden # n_hidden ???????????????emb???????????????
        self.d = torch.nn.Parameter(torch.ones(size=(n_hidden, ))) # ???????????????????????????self.d???????????????????????????tensor, ?????????????????????1?????????tensor????????????n_hidden
        self.scale = torch.nn.Parameter(torch.full(size=(1, ), fill_value= 0.5)) # ???????????????????????????self.scale???????????????????????????tensor, ?????????????????????0.5?????????tensor????????????1

    def forward(self, emb, g):
        print('[node-level/stu-gcn/train.py] ????????????local_emb_D???forward?????? ????????????emb.shape:', emb.shape, 'g:', g)
        """ ????????????emb.shape: torch.Size([2708, 64]) 
        g: Graph(num_nodes=2708, num_edges=13264,
            ndata_schemes={'feat': Scheme(shape=(1433,), dtype=torch.float32), 'label': Scheme(shape=(), dtype=torch.int64), 'test_mask': Scheme(shape=(), dtype=torch.bool), 'train_mask': Scheme(shape=(), dtype=torch.bool), 'val_mask': Scheme(shape=(), dtype=torch.bool), 'norm': Scheme(shape=(1,), dtype=torch.float32)}
            edata_schemes={}) """
        emb = F.normalize(emb, p=2)
        #print('[node-level/stu-gcn/train.py] ????????????local_emb_D???forward?????? ??????emb = F.normalize(emb, p=2)????????????emb.shape:', emb.shape) # emb.shape: torch.Size([2708, 64])
        g.ndata['e'] = emb # e?????? embedding, ndata?????? node data, g.ndata['e']???????????????emb?????????g?????????????????????e??????
        g.ndata['ew'] = emb @ torch.diag(self.d) # @?????????????????????, torch.diag(self.d)??????self.d?????????????????????, self.d??????????????????tensor ew?????? embedding weight
        g.apply_edges(fn.u_dot_v('ew', 'e', 'z')) # ???????????????????????????g?????????????????????????????????embedding weight?????????????????????????????????z???, fn.u_dot_v???????????????????????????????????????????????????embedding weight??????
        pair_dis = g.edata['z'] # pair_dis?????? pair distance???edata???edge data
        print('[node-level/stu-gcn/train.py] ????????????local_emb_D???forward?????? forward???????????????(pair_dis * self.scale).shape:', (pair_dis * self.scale).shape) # torch.Size([13264, 1])
        return pair_dis * self.scale

class global_emb_D(torch.nn.Module): # ????????????????????? ????e_global??? patch-summary pair to the real value that we interpret as affinity between node and graph.
    def __init__(self, n_hidden): 
        super(global_emb_D, self).__init__()
        self.n_hidden = n_hidden # 16
        self.d = torch.nn.Parameter(torch.ones(size=(n_hidden, )))
        self.scale = torch.nn.Parameter(torch.full(size=(1, ), fill_value= 0.5))
        """ print(f'[node-level/stu-gcn/train.py] ????????????global_emb_D???__init__?????? self.d.shape: {self.d.shape}, self.scale.shape: {self.scale.shape}, self.d: {self.d}, self.scale: {self.scale}')
        self.d.shape: torch.Size([16]), self.scale.shape: torch.Size([1]), self.d: Parameter containing: tensor([1., 1., 1., 1., 1., 1., 1., 1., 1., 1., 1., 1., 1., 1., 1., 1.], requires_grad=True), 
        self.scale: Parameter containing: tensor([0.5000], requires_grad=True) """

    def forward(self, emb, summary):
        # print('[node-level/stu-gcn/train.py] ????????????global_emb_D???forward?????? ????????????emb.shape:', emb.shape, 'summary.shape:', summary.shape) # ????????????emb.shape: torch.Size([128, 16]) summary.shape: torch.Size([16, 1])
        # print(f'[node-level/stu-gcn/train.py] ????????????global_emb_D???forward?????? ???????????? emb: {emb}, summary: {summary}')
        """ emb: tensor([[-2.3813, -0.1555,  0.0000,  ...,  0.0000,  0.0000, -0.7409],
                        [-1.7513, -0.0595,  0.0000,  ...,  0.4415,  0.0000, -0.4628],
                        [-2.5431, -0.0896,  0.0000,  ...,  0.4133,  0.0000, -1.2693],
                        ...,
                        [-1.9433, -0.0490,  0.0000,  ...,  0.1811,  0.0000, -0.4383],
                        [-2.0101, -0.5077,  0.0000,  ...,  0.3433,  0.0000, -0.6192],
                        [-1.9890, -0.2984,  0.0000,  ...,  0.6872,  0.0000, -0.3338]]), 
        summary: tensor([[0.1196],
                        [0.3883],
                        [0.5000],
                        [0.5000],
                        [0.2013],
                        [0.7673],
                        [0.5000],
                        [0.8395],
                        [0.5920],
                        [0.4463],
                        [0.9244],
                        [0.5524],
                        [0.5003],
                        [0.5758],
                        [0.5000],
                        [0.3813]]) """
    
        emb = F.normalize(emb, p=2)
        # print('[node-level/stu-gcn/train.py] ????????????global_emb_D???forward?????? ??????emb = F.normalize(emb, p=2)????????????emb.shape:', emb.shape) # ??????emb.shape: torch.Size([128, 16])
        # print(f'[node-level/stu-gcn/train.py] ????????????global_emb_D???forward?????? ??????emb = F.normalize(emb, p=2)????????????emb: {emb}')
        
        
        sim = emb @ torch.diag(self.d) #sim?????? similarity, torch.diag ??????self.d????????????????????????
        # print('[node-level/stu-gcn/train.py] ????????????global_emb_D???forward?????? ??????sim = emb @ torch.diag(self.d)????????????sim.shape:', sim.shape) # ??????sim.shape: torch.Size([128, 16])
        # print(f'[node-level/stu-gcn/train.py] ????????????global_emb_D???forward?????? ??????sim = emb @ torch.diag(self.d)????????????sim: {sim}')

        assert summary.shape[-1] == 1 # summary.shape[-1] == 1????????????summary???????????????????????????1
        sim = sim @ summary # @?????????????????????
        # print('[node-level/stu-gcn/train.py] ????????????global_emb_D???forward?????? ??????sim = sim @ summary????????????sim.shape:', sim.shape, '???????????????sim * self.scale.shape:', (sim * self.scale).shape) # ??????sim.shape: torch.Size([128, 1]) ??????????????? sim * self.scale.shape: torch.Size([128, 1])
        return sim * self.scale
