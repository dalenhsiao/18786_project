import torch
import torch.nn as nn
import math
from torch_geometric.nn import NNConv, GATConv, GCNConv
import torch.nn.functional as F
from tqdm import tqdm
from utils import *

# Graph Convolution Layer
class GraphConv(nn.Module):
    def __init__(self, in_channels, out_channels, time_emb_dim, edge_attr_dim=0, aggr="mean",activation=nn.SiLU(), attention=False):
        super(GraphConv, self).__init__()
        self.activation = activation
        if edge_attr_dim > 0:
            nn_model = nn.Sequential(
                    nn.Linear(edge_attr_dim, 32), 
                    self.activation,
                    nn.Linear(32, in_channels*in_channels)
            )
            self.gc_edge_attr = NNConv(in_channels, in_channels, nn_model, aggr)
            self.linear = nn.Linear(in_channels, out_channels)
        
        
        self.time_mlp = nn.Linear(time_emb_dim, out_channels) # (n_sample, out_channels)
        self.layer_norm = nn.LayerNorm(out_channels)
        self.layer_norm2 = nn.LayerNorm(out_channels)

        
        if attention: 
            self.gc = GATConv(in_channels, out_channels)
        else:
            self.gc = GCNConv(in_channels, out_channels)
        
        self.lin = nn.Linear(out_channels, out_channels)
            
        
    
    # if edge_attr is None, will not convolve the edge attributes 
    def forward(self, x, edge_index,t, edge_attr=None):
        if edge_attr is None:
            x = self.activation(self.gc(x,edge_index))
        else:
            x = self.activation(self.gc_edge_attr(x, edge_index, edge_attr))
            x = self.linear(x)
            x = self.activation(self.layer_norm(x))
            
            
        time_emb = self.activation(self.time_mlp(t))
        # print("shape x: ", x.shape)
        # print("shape time_emb: ", time_emb.shape)
        x += time_emb
        x = self.layer_norm2(self.activation(self.lin(x)))
        
        return x

# for time embedding (Sinusoidal time embedding)
class SinusoidalPositionEmbeddings(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.dim = dim

    def forward(self, time):
        device = time.device
        half_dim = self.dim // 2
        embeddings = math.log(10000) / (half_dim - 1)
        embeddings = torch.exp(torch.arange(half_dim, device=device) * -embeddings)
        embeddings = time[:, None] * embeddings[None, :]
        embeddings = torch.cat((embeddings.sin(), embeddings.cos()), dim=-1)
        # TODO: Double check the ordering here
        return embeddings

# edge attributes NN
class NNEdgeAttr(nn.Module):
    def __init__(self, edge_in_dim, edge_out_dim, act):
        super(NNEdgeAttr, self).__init__()
        # if edge_attr:
        self.act = act 
        self.self.edge_attr_nn = nn.Sequential(
                nn.Linear(edge_in_dim, edge_out_dim), 
                self.act,
                nn.Linear(edge_out_dim, edge_out_dim*edge_out_dim)
                ) # NN convolution block for edge conditioned convolution
    def forward(self,x):
        out = self.edge_attr_nn(x)
        return out


# Encoder 
class Net(nn.Module):
    def __init__(
        self,
        n_feat_in,
        layers,
        time_emb_dim,
        activation= nn.SiLU(),
        edge_attr_dim = 0,
        fine_tune = False,
        freeze_pretrain = False
    ):
        super(Net, self).__init__()
        """
        n_feat_in: number of features input 
        layers: model structure 
        time_emb_dim: time embedding dimension 
        activation: activation function 
        edge_attr_dim: edge attributes dimensions (default 0 for not using edge attributes)
        """
        # self.device = device
        # self.n_layers = n_layers
        self.layers = layers
        self.act = activation
        self.fine_tune = fine_tune
        self.layer0 = nn.Linear(n_feat_in, self.layers[0]) # initialize the embedding layer
        self.layer_out = nn.Linear(self.layers[0], n_feat_in) # output embedding layer (latent space)
        self.freeze = freeze_pretrain
        if self.fine_tune:
            self.last_layer_new = nn.Linear(self.layers[0], n_feat_in)
        self.edge_attr_dim = edge_attr_dim
        self.time_mlp = nn.Sequential(
                SinusoidalPositionEmbeddings(time_emb_dim),
                nn.Linear(time_emb_dim, time_emb_dim),
                nn.ReLU() # maybe modify?
            )
        ### Encoder (Downsampling)
        # graph convolution layers
        self.downsampling = nn.ModuleList([
            GraphConv(in_channels=self.layers[i], out_channels=self.layers[i+1], time_emb_dim=time_emb_dim, edge_attr_dim = self.edge_attr_dim, aggr='add', activation=self.act)
            for i in range(0,len(self.layers)-1)
        ]
        )
        ### Decoder (Upsampling)
        self.upsampling = nn.ModuleList([
            GraphConv(in_channels=self.layers[i], out_channels=self.layers[i-1],time_emb_dim=time_emb_dim, edge_attr_dim=self.edge_attr_dim, aggr='add', activation=self.act)
            for i in reversed(range(1, len(self.layers)))
        ])
        # Freeze pretrain parameters
        if self.fine_tune and self.freeze:
            for params in self.layer0.parameters():
                params.requires_grad = False
            for params in self.downsampling.parameters():
                params.requires_grad = False
            for params in self.upsampling.parameters():
                params.requires_grad = False
            for params in self.layer_out.parameters():
                params.requires_grad = False
        
        self.act = nn.LogSoftmax(dim=1)
            
        # Pooling layers
        """
        Basically the parameters
        
        graph convolution:
        - forward pass: (x:nodes embedding, edge_index: edge index, edge_attr: edge attributes)
        
        """

    def forward(self, x, timestep, edge_index, edge_attr=None):
        """_summary_

        Args:
            x (_type_): node features (node embedding with nn.Embedding)
            timestep (_type_): time step for noising (n_batch, )
            edge_index (_type_): edge index matrix, shape = [2, num_edges]
            edge_attr (_type_): edge attributes, default None


        Returns:
            h: graph latent representation
        """
        # min-max scaling
        x = min_max_scale(x) # 0 and 1 
        h = self.layer0(x) # initialize node embedding
        t = self.time_mlp(timestep)
        # downsampling 
        for down in self.downsampling:
            h = down(h, edge_index, t, edge_attr)
        # upsampling 
        for up in self.upsampling:
            h = up(h, edge_index, t, edge_attr)
        if self.fine_tune:
            out = self.last_layer_new(h)
        else:  
            logits = self.layer_out(h)
            out = self.act(logits) # converting the logits to probability
        return out


class LatentSpace(nn.Module):
    def __init__(
        self, 
        layers: list,
        activation: nn.Module = nn.SiLU()
        ):
        super(LatentSpace, self).__init__()
        self.activation = activation
        self.layers = layers
        self.struc = nn.ModuleList(
            [
                self.block(self.layers[i], self.layers[i+1])
                for i in range(0, len(self.layers) - 2)
                ]
        )
        self.out_layer = nn.Linear(self.layers[-2], self.layers[-1])
    
    def block(self, in_dim, out_dim):
        return nn.Sequential(
            nn.Linear(in_dim, out_dim),
            self.activation
        )
    
    def forward(self, z):
        for layer in self.struc:
            z = layer(z)
        out = self.out_layer(z)
        return out


class GNN(nn.Module):
    # def __init__(self, n_feat_in, hidden_dim, latent_dim, time_emb_dim,n_layers, activation= nn.SiLU(), edge_attr_dim = None):
    def __init__(
        self,
        n_feat_in,
        layers: list,
        latent_space_dims: list,
        time_emb_dim: int,
        activation = nn.SiLU(),
        edge_attr_dim: int = 0,
        fine_tune = False,
        freeze_pretrain = False
    ):
        super(GNN, self).__init__()
        """
        n_feat_in: number of features input 
        layers: model structure 
        time_emb_dim: time embedding dimension 
        activation: activation function 
        edge_attr_dim: edge attributes dimensions (default 0 for not using edge attributes)
        """
        # self.device = device
        # self.n_layers = n_layers
        self.layers = layers
        self.act = activation
        self.latent_space_dims = latent_space_dims
        self.fine_tune = fine_tune
        self.layer0 = nn.Linear(n_feat_in, self.layers[0]) # initialize the embedding layer
        self.layer_out = nn.Linear(self.layers[0], n_feat_in) # output embedding layer (latent space)
        self.freeze = freeze_pretrain
        if self.fine_tune:
            self.last_layer_new = nn.Linear(self.layers[0], n_feat_in)
        self.edge_attr_dim = edge_attr_dim
        self.time_mlp = nn.Sequential(
                SinusoidalPositionEmbeddings(time_emb_dim),
                nn.Linear(time_emb_dim, time_emb_dim),
                nn.ReLU() # maybe modify?
            )
        ### Encoder (Downsampling)
        # graph convolution layers
        self.downsampling = nn.ModuleList([
            GraphConv(in_channels=self.layers[i],
                      out_channels=self.layers[i+1],
                      time_emb_dim=time_emb_dim,
                      edge_attr_dim = self.edge_attr_dim,
                      aggr='add',
                      activation=self.act
            )
            for i in range(0,len(self.layers)-1)
        ]
        )
        
        ### latent space (where noise is added)
        self.latent = LatentSpace(
            [self.layers[-1], *self.latent_space_dims, self.layers[-1]],
            activation=self.act
        )
        
        ### Decoder (Upsampling)
        self.upsampling = nn.ModuleList([
            GraphConv(
                in_channels=self.layers[i],
                out_channels=self.layers[i-1],
                time_emb_dim=time_emb_dim,
                edge_attr_dim=self.edge_attr_dim,
                aggr='add',
                activation=self.act
                )
            for i in reversed(range(1, len(self.layers)))
        ])
        # Freeze pretrain parameters
        if self.fine_tune and self.freeze:
            for params in self.layer0.parameters():
                params.requires_grad = False
            for params in self.downsampling.parameters():
                params.requires_grad = False
            for params in self.upsampling.parameters():
                params.requires_grad = False
            for params in self.layer_out.parameters():
                params.requires_grad = False
        
        self.act = nn.LogSoftmax(dim=1)
            
        # Pooling layers
        """
        Basically the parameters
        
        graph convolution:
        - forward pass: (x:nodes embedding, edge_index: edge index, edge_attr: edge attributes)
        
        """

    def forward(self, x, timestep, edge_index, edge_attr=None):
        """_summary_
        The outer GCN is a graph-to-graph model used to decypher the information in graph data,
        comprised of GCN layers.
        
        The inner structure is a MLP structure used to learn the noise of the diffusion,
        the input is the latent representation of the Graph data output by the downsampling
        process; the output data is also latent representation to be passed to upsampling of GCN.
        """
        # min-max scaling
        x = min_max_scale(x) # 0 and 1 
        h = self.layer0(x) # initialize node embedding
        t = self.time_mlp(timestep)
        # downsampling 
        for down in self.downsampling:
            h = down(h, edge_index, t, edge_attr)
        # latent space]
        self.z0 = h
        self.z = self.latent(h)
        h = self.z
        # upsampling 
        for up in self.upsampling:
            h = up(h, edge_index, t, edge_attr)
        logits = self.layer_out(h)
        return logits
    
    def get_loss(
        self,
        logits,
        graph_gt,
        # beta: int = 0.5,
        # mlp_loss_fun: nn.Module = nn.MSELoss(),
        graph_loss_fun: nn.Module = nn.CrossEntropyLoss()
    ):
        # mlp_loss = mlp_loss_fun(self.z, self.z0)
        graph_loss = graph_loss_fun(logits, graph_gt)
        return graph_loss
