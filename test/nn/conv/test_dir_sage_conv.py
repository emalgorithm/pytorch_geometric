import copy

import torch

from torch_geometric.nn import DirSageConv
from torch_geometric.testing import is_full_test


def test_dir_sage_conv():
    # x = torch.randn(4, 16)
    # edge_index = torch.tensor([[0, 0, 0, 1, 2, 3], [1, 2, 3, 0, 0, 0]])
    # value = torch.rand(edge_index.size(1))
    # adj1 = to_torch_csc_tensor(edge_index, size=(4, 4))
    # adj2 = to_torch_csc_tensor(edge_index, value, size=(4, 4))

    conv = DirSageConv(16, 32)
    assert str(conv) == 'DirSageConv(16, 32)'

    # out1 = conv(x, edge_index)
    # assert out1.size() == (4, 32)
    # assert torch.allclose(conv(x, adj1.t()), out1, atol=1e-6)

    # out2 = conv(x, edge_index, value)
    # assert out2.size() == (4, 32)
    # assert torch.allclose(conv(x, adj2.t()), out2, atol=1e-6)

    # if torch_geometric.typing.WITH_TORCH_SPARSE:
    #     adj3 = SparseTensor.from_edge_index(edge_index, sparse_sizes=(4, 4))
    #     adj4 = SparseTensor.from_edge_index(edge_index, value, (4, 4))
    #     assert torch.allclose(conv(x, adj3.t()), out1, atol=1e-6)
    #     assert torch.allclose(conv(x, adj4.t()), out2, atol=1e-6)

    # if is_full_test():
    #     t = '(Tensor, Tensor, OptTensor) -> Tensor'
    #     jit = torch.jit.script(conv.jittable(t))
    #     assert torch.allclose(jit(x, edge_index), out1, atol=1e-6)
    #     assert torch.allclose(jit(x, edge_index, value), out2, atol=1e-6)

    # if is_full_test() and torch_geometric.typing.WITH_TORCH_SPARSE:
    #     t = '(Tensor, SparseTensor, OptTensor) -> Tensor'
    #     jit = torch.jit.script(conv.jittable(t))
    #     assert torch.allclose(jit(x, adj3.t()), out1, atol=1e-6)
    #     assert torch.allclose(jit(x, adj4.t()), out2, atol=1e-6)


def test_dir_sage_conv_with_decomposed_layers():
    x = torch.randn(4, 16)
    edge_index = torch.tensor([[0, 0, 0, 1, 2, 3], [1, 2, 3, 0, 0, 0]])

    conv = DirSageConv(16, 32)

    decomposed_conv = copy.deepcopy(conv)
    decomposed_conv.decomposed_layers = 2

    out1 = conv(x, edge_index)
    out2 = decomposed_conv(x, edge_index)
    assert torch.allclose(out1, out2)

    if is_full_test():
        t = '(Tensor, Tensor, OptTensor) -> Tensor'
        jit = torch.jit.script(decomposed_conv.jittable(t))
        assert torch.allclose(jit(x, edge_index), out1)


def test_static_dir_sage_conv():
    x = torch.randn(3, 4, 16)
    edge_index = torch.tensor([[0, 0, 0, 1, 2, 3], [1, 2, 3, 0, 0, 0]])

    conv = DirSageConv(16, 32)
    out = conv(x, edge_index)
    assert out.size() == (3, 4, 32)
