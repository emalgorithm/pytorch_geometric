import copy
import os.path as osp
import sys
from dataclasses import dataclass
from typing import List, Optional

import torch
import torch.utils.data
from torch import Tensor

import torch_geometric.typing
from torch_geometric.data import Data
from torch_geometric.typing import pyg_lib
from torch_geometric.utils import index_sort, narrow, select, sort_edge_index
from torch_geometric.utils.map import map_index
from torch_geometric.utils.sparse import index2ptr, ptr2index


@dataclass
class Partition:
    rowptr: Tensor
    col: Tensor
    partptr: Tensor
    node_perm: Tensor
    edge_perm: Tensor


class ClusterData(torch.utils.data.Dataset):
    r"""Clusters/partitions a graph data object into multiple subgraphs, as
    motivated by the `"Cluster-GCN: An Efficient Algorithm for Training Deep
    and Large Graph Convolutional Networks"
    <https://arxiv.org/abs/1905.07953>`_ paper.

    .. note::
        The underlying METIS algorithm requires undirected graphs as input.

    Args:
        data (torch_geometric.data.Data): The graph data object.
        num_parts (int): The number of partitions.
        recursive (bool, optional): If set to :obj:`True`, will use multilevel
            recursive bisection instead of multilevel k-way partitioning.
            (default: :obj:`False`)
        save_dir (str, optional): If set, will save the partitioned data to the
            :obj:`save_dir` directory for faster re-use. (default: :obj:`None`)
        log (bool, optional): If set to :obj:`False`, will not log any
            progress. (default: :obj:`True`)
        keep_inter_cluster_edges (bool, optional): If set to :obj:`True`,
            will keep inter-cluster edge connections. (default: :obj:`False`)
    """
    def __init__(
        self,
        data,
        num_parts: int,
        recursive: bool = False,
        save_dir: Optional[str] = None,
        log: bool = True,
        keep_inter_cluster_edges: bool = False,
    ):
        assert data.edge_index is not None

        self.num_parts = num_parts
        self.recursive = recursive
        self.keep_inter_cluster_edges = keep_inter_cluster_edges

        recursive_str = '_recursive' if recursive else ''
        filename = f'metis_{num_parts}{recursive_str}.pt'
        path = osp.join(save_dir or '', filename)
        if save_dir is not None and osp.exists(path):
            self.partition = torch.load(path)
        else:
            if log:  # pragma: no cover
                print('Computing METIS partitioning...', file=sys.stderr)

            cluster = self._metis(data.edge_index, data.num_nodes)
            self.partition = self._partition(data.edge_index, cluster)

            if save_dir is not None:
                torch.save(self.partition, path)

            if log:  # pragma: no cover
                print('Done!', file=sys.stderr)

        self.data = self._permute_data(data, self.partition)

    def _metis(self, edge_index: Tensor, num_nodes: int) -> Tensor:
        # Computes a node-level partition assignment vector via METIS.

        # Calculate CSR representation:
        row, col = sort_edge_index(edge_index, num_nodes=num_nodes)
        rowptr = index2ptr(row, size=num_nodes)

        # Compute METIS partitioning:
        if torch_geometric.typing.WITH_METIS:
            return pyg_lib.partition.metis(
                rowptr.cpu(),
                col.cpu(),
                self.num_parts,
                recursive=self.recursive,
            ).to(edge_index.device)

        if torch_geometric.typing.WITH_TORCH_SPARSE:
            return torch.ops.torch_sparse.partition(
                rowptr.cpu(),
                col.cpu(),
                None,
                self.num_parts,
                self.recursive,
            ).to(edge_index.device)

        raise ImportError(f"'{self.__class__.__name__}' requires either "
                          f"'pyg-lib' or 'torch-sparse'")

    def _partition(self, edge_index: Tensor, cluster: Tensor) -> Partition:
        # Computes node-level and edge-level permutations and permutes the edge
        # connectivity accordingly:

        # Sort `cluster` and compute boundaries `partptr`:
        cluster, node_perm = index_sort(cluster, max_value=self.num_parts)
        partptr = index2ptr(cluster, size=self.num_parts)

        # Permute `edge_index` based on node permutation:
        edge_perm = torch.arange(edge_index.size(1), device=edge_index.device)
        arange = torch.empty_like(node_perm)
        arange[node_perm] = torch.arange(cluster.numel(),
                                         device=cluster.device)
        edge_index = arange[edge_index]

        # Compute final CSR representation:
        (row, col), edge_perm = sort_edge_index(
            edge_index,
            edge_attr=edge_perm,
            num_nodes=cluster.numel(),
        )
        rowptr = index2ptr(row, size=cluster.numel())

        return Partition(rowptr, col, partptr, node_perm, edge_perm)

    def _permute_data(self, data: Data, partition: Partition) -> Data:
        # Permute node-level and edge-level attributes according to the
        # calculated permutations in `Partition`:
        out = copy.copy(data)
        for key, value in data.items():
            if key == 'edge_index':
                continue
            elif data.is_node_attr(key):
                cat_dim = data.__cat_dim__(key, value)
                out[key] = select(value, partition.node_perm, dim=cat_dim)
            elif data.is_edge_attr(key):
                cat_dim = data.__cat_dim__(key, value)
                out[key] = select(value, partition.edge_perm, dim=cat_dim)
        out.edge_index = None

        return out

    def __len__(self) -> int:
        return self.partition.partptr.numel() - 1

    def __getitem__(self, idx: int) -> Data:
        node_start = int(self.partition.partptr[idx])
        node_end = int(self.partition.partptr[idx + 1])
        node_length = node_end - node_start

        rowptr = self.partition.rowptr[node_start:node_end + 1]
        edge_start = int(rowptr[0])
        edge_end = int(rowptr[-1])
        edge_length = edge_end - edge_start
        rowptr = rowptr - edge_start
        row = ptr2index(rowptr)
        col = self.partition.col[edge_start:edge_end]
        if not self.keep_inter_cluster_edges:
            edge_mask = (col >= node_start) & (col < node_end)
            row = row[edge_mask]
            col = col[edge_mask] - node_start

        out = copy.copy(self.data)

        for key, value in self.data.items():
            if key == 'num_nodes':
                out.num_nodes = node_length
            elif self.data.is_node_attr(key):
                cat_dim = self.data.__cat_dim__(key, value)
                out[key] = narrow(value, cat_dim, node_start, node_length)
            elif self.data.is_edge_attr(key):
                cat_dim = self.data.__cat_dim__(key, value)
                out[key] = narrow(value, cat_dim, edge_start, edge_length)
                if not self.keep_inter_cluster_edges:
                    out[key] = out[key][edge_mask]

        out.edge_index = torch.stack([row, col], dim=0)

        return out

    def __repr__(self) -> str:
        return f'{self.__class__.__name__}({self.num_parts})'


class ClusterLoader(torch.utils.data.DataLoader):
    r"""The data loader scheme from the `"Cluster-GCN: An Efficient Algorithm
    for Training Deep and Large Graph Convolutional Networks"
    <https://arxiv.org/abs/1905.07953>`_ paper which merges partioned subgraphs
    and their between-cluster links from a large-scale graph data object to
    form a mini-batch.

    .. note::

        Use :class:`~torch_geometric.loader.ClusterData` and
        :class:`~torch_geometric.loader.ClusterLoader` in conjunction to
        form mini-batches of clusters.
        For an example of using Cluster-GCN, see
        `examples/cluster_gcn_reddit.py <https://github.com/pyg-team/
        pytorch_geometric/blob/master/examples/cluster_gcn_reddit.py>`_ or
        `examples/cluster_gcn_ppi.py <https://github.com/pyg-team/
        pytorch_geometric/blob/master/examples/cluster_gcn_ppi.py>`_.

    Args:
        cluster_data (torch_geometric.loader.ClusterData): The already
            partioned data object.
        **kwargs (optional): Additional arguments of
            :class:`torch.utils.data.DataLoader`, such as :obj:`batch_size`,
            :obj:`shuffle`, :obj:`drop_last` or :obj:`num_workers`.
    """
    def __init__(self, cluster_data, **kwargs):
        self.cluster_data = cluster_data
        iterator = range(len(cluster_data))
        super().__init__(iterator, collate_fn=self._collate, **kwargs)

    def _collate(self, batch: List[int]) -> Data:
        if not isinstance(batch, torch.Tensor):
            batch = torch.tensor(batch)

        global_rowptr = self.cluster_data.partition.rowptr
        global_col = self.cluster_data.partition.col

        # Get all node-level and edge-level start and end indices for the
        # current mini-batch:
        node_start = self.cluster_data.partition.partptr[batch]
        node_end = self.cluster_data.partition.partptr[batch + 1]
        edge_start = global_rowptr[node_start]
        edge_end = global_rowptr[node_end]

        # Iterate over each partition in the batch and calculate new edge
        # connectivity. This is done by slicing the corresponding source and
        # destination indices for each partition and adjusting their indices to
        # start from zero:
        rows, cols, nodes, cumsum = [], [], [], 0
        for i in range(batch.numel()):
            nodes.append(torch.arange(node_start[i], node_end[i]))
            rowptr = global_rowptr[node_start[i]:node_end[i] + 1]
            rowptr = rowptr - edge_start[i]
            row = ptr2index(rowptr) + cumsum
            col = global_col[edge_start[i]:edge_end[i]]
            rows.append(row)
            cols.append(col)
            cumsum += rowptr.numel() - 1

        node = torch.cat(nodes, dim=0)
        row = torch.cat(rows, dim=0)
        col = torch.cat(cols, dim=0)

        # Map `col` vector to valid entries and remove any entries that do not
        # connect two nodes within the same mini-batch:
        col, edge_mask = map_index(col, node)
        row = row[edge_mask]

        out = copy.copy(self.cluster_data.data)

        # Slice node-level and edge-level attributes according to its offsets:
        for key, value in self.cluster_data.data.items():
            if key == 'num_nodes':
                out.num_nodes = cumsum
            elif self.cluster_data.data.is_node_attr(key):
                cat_dim = self.cluster_data.data.__cat_dim__(key, value)
                out[key] = torch.cat([
                    narrow(out[key], cat_dim, s, e - s)
                    for s, e in zip(node_start, node_end)
                ], dim=cat_dim)
            elif self.cluster_data.data.is_edge_attr(key):
                cat_dim = self.cluster_data.data.__cat_dim__(key, value)
                value = torch.cat([
                    narrow(out[key], cat_dim, s, e - s)
                    for s, e in zip(edge_start, edge_end)
                ], dim=cat_dim)
                out[key] = select(value, edge_mask, dim=cat_dim)

        out.edge_index = torch.stack([row, col], dim=0)

        return out
