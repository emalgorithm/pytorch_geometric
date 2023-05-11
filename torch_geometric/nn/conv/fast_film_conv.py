import copy
from typing import Callable, Optional, Tuple, Union

from torch import Tensor
import torch
from torch.nn import ModuleList, ReLU

from torch_geometric.nn.conv import MessagePassing
from torch_geometric.nn.dense.linear import Linear, HeteroLinear
from torch_geometric.nn.inits import reset
from torch_geometric.typing import (
    Adj,
    OptTensor,
    PairTensor,
    SparseTensor,
    torch_sparse,
)
from torch_geometric.utils import is_sparse, to_edge_index


class FastFiLMConv(MessagePassing):
    r"""See :class:`FiLMConv`."""
    def __init__(
            self,
            in_channels: Union[int, Tuple[int, int]],
            out_channels: int,
            num_relations: int = 1,
            nn: Optional[Callable] = None,
            act: Optional[Callable] = ReLU(),
            aggr: str = 'mean',
            **kwargs,
    ):
        super().__init__(aggr=aggr, **kwargs)

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.num_relations = max(num_relations, 1)
        self.act = act
        self.nn_is_none = nn is None
        if isinstance(in_channels, int):
            in_channels = (in_channels, in_channels)

        # self.lins = ModuleList()
        # self.films = ModuleList()
        if self.num_relations > 1:
            self.lins = HeteroLinear(in_channels[0], out_channels, num_types=num_relations, is_sorted=True, bias=False)
            if self.nn_is_none:
                self.films = HeteroLinear(in_channels[1], 2* out_channels, num_types=num_relations, is_sorted=True, bias=False)
            else:
                self.films = ModuleList()
                for _ in range(num_relations):
                    self.films.append(copy.deepcopy(nn))
        else:
            self.lins = (Linear(in_channels[0], out_channels, bias=False))
            if self.nn_is_none:
                self.films = Linear(in_channels[1], 2 * out_channels)
            else:
                self.films = copy.deepcopy(nn)
        # for _ in range(num_relations):
        #     self.lins.append(Linear(in_channels[0], out_channels, bias=False))
        #     if nn is None:
        #         film = Linear(in_channels[1], 2 * out_channels)
        #     else:
        #         film = copy.deepcopy(nn)
        #     self.films.append(film)
        self.lin_skip = Linear(in_channels[1], self.out_channels, bias=False)
        if self.nn_is_none:
            self.film_skip = Linear(in_channels[1], 2 * self.out_channels,
                                    bias=False)
        else:
            self.film_skip = copy.deepcopy(nn)

        self.reset_parameters()

    def reset_parameters(self):
        super().reset_parameters()
        self.lins.reset_parameters()
        if self.nn_is_none:
            self.films.reset_parameters()
        else:
            for f in self.films:
                reset(f)
        self.film_skip.reset_parameters()
        self.lin_skip.reset_parameters()
        reset(self.film_skip)

    def forward(self, x: Union[Tensor, PairTensor], edge_index: Adj,
                edge_type: OptTensor = None) -> Tensor:

        if isinstance(x, Tensor):
            x: PairTensor = (x, x)
        print("entered fastfilmforward")
        beta, gamma = self.film_skip(x[1]).split(self.out_channels, dim=-1)
        print("finished film_skip")
        out = gamma * self.lin_skip(x[1]) + beta
        print("finished lin_skip")
        if self.act is not None:
            out = self.act(out)

        # propagate_type: (x: Tensor, beta: Tensor, gamma: Tensor)
        if self.num_relations <= 1:
            beta, gamma = self.films(x[1]).split(self.out_channels, dim=-1)
            out = out + self.propagate(edge_index, x=self.lins(x[0]),
                                       beta=beta, gamma=gamma, size=None)
        else:
            # (TODO) add support for sparse tensors without conversion
            if is_sparse(edge_index):
                print("Warning: sparse edge representations are not supported for FastFiLMConv yet.\
                       This incurs an additional conversion each forward pass.")
                edge_index = to_edge_index(edge_index)[0]
            film_xs = []
            propogate_xs = []
            type_list = []
            print("about to for-loop")
            for e_type_i in range(self.num_relations):
                edge_mask = edge_type == e_type_i
                masked_src_idxs, masked_dst_idxs = edge_index[:, edge_mask]
                N = masked_src_idxs.numel()
                type_list.append(torch.full((N, ), e_type_i, dtype=torch.long))
                # make film xs list
                film_x = x[1][masked_dst_idxs, :]
                film_xs.append(film_x)
                # make make propogate xs list
                propogate_x = x[0][masked_src_idxs, :]
                propogate_xs.append(propogate_x)
            type_vec = torch.cat(type_list)
            # cat and apply linears
            print("catting")
            film_x = torch.cat(film_xs)
            print("about to films")
            print("film_x.size()=", film_x.size())
            print("type_vec.size()=", type_vec.size())
            print("out_channels=", self.out_channels)
            print("self.films=", self.films)
            print("type_vec=", type_vec)
            film_o = self.films(film_x, type_vec)
            print("splitting film_o")
            beta, gamma = film_o.split(self.out_channels, dim=-1)
            print("about to lins")
            propogate_x = self.lins(torch.cat(propogate_xs), type_vec)
            # propogate
            print("about to prop")
            print("out.size()=", out.size())
            print("propogate_x.size()=", propogate_x.size())
            print("edge_index.size()=", edge_index.size())
            print("beta.size()=", beta.size())
            print("gamma.size()=", gamma.size())
            print("edge_type=", edge_type)
            prop_out = self.propagate(edge_index, x=propogate_x, beta=beta, gamma=gamma, size=None)
            print("prop_out.size()=", prop_out.size())
            out += prop_out

        # if self.num_relations <= 1:
        #     beta, gamma = self.films[0](x[1]).split(self.out_channels, dim=-1)
        #     out = out + self.propagate(edge_index, x=self.lins[0](x[0]),
        #                                beta=beta, gamma=gamma, size=None)
        # else:
        #     for i, (lin, film) in enumerate(zip(self.lins, self.films)):
        #         beta, gamma = film(x[1]).split(self.out_channels, dim=-1)
        #         if isinstance(edge_index, SparseTensor):
        #             edge_type = edge_index.storage.value()
        #             assert edge_type is not None
        #             mask = edge_type == i
        #             adj_t = torch_sparse.masked_select_nnz(
        #                 edge_index, mask, layout='coo')
        #             out = out + self.propagate(adj_t, x=lin(x[0]), beta=beta,
        #                                        gamma=gamma, size=None)
        #         else:
        #             assert edge_type is not None
        #             mask = edge_type == i
        #             out = out + self.propagate(edge_index[:, mask], x=lin(
        #                 x[0]), beta=beta, gamma=gamma, size=None)

        return out

    def message(self, x_j: Tensor, beta_i: Tensor, gamma_i: Tensor) -> Tensor:
        out = gamma_i * x_j + beta_i
        if self.act is not None:
            out = self.act(out)
        return out

    def __repr__(self) -> str:
        return (f'{self.__class__.__name__}({self.in_channels}, '
                f'{self.out_channels}, num_relations={self.num_relations})')