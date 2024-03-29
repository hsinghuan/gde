import argparse
import os
import numpy as np
from copy import deepcopy
from collections import OrderedDict
import torch
import torch.nn.functional as F
from torchsummary import summary
from utils import get_device, set_random_seeds, eval
import dataset
from adapter import *
from model import TwoLayerCNN, ThreeLayerCNN, OneLayerMLPEnc, TwoLayerMLPHead, OneLayerMLPHead, Model

get_dataloader = {"rotate-mnist": dataset.get_rotate_mnist,
                  "portraits": dataset.get_portraits,
                  "covertype": dataset.get_covertype}

get_domain = {"rotate-mnist": dataset.rotate_mnist_domains,
              "portraits": dataset.portraits_domains,
              "covertype": dataset.covertype_domains}

get_total_train_num = {"rotate-mnist": dataset.rotate_mnist_total_train_num,
                       "portraits": dataset.portraits_total_train_num,
                       "covertype": dataset.covertype_total_train_num}

get_class_num = {"rotate-mnist": dataset.rotate_mnist_class_num,
                 "portraits": dataset.portraits_class_num,
                 "covertype": dataset.covertype_class_num}

def train(loader, encoder, head, optimizer, device="cpu"):
    encoder.train()
    head.train()
    total_loss = 0
    total_correct = 0
    total_num = 0
    for data, y in loader:
        data, y = data.to(device), y.to(device)
        output = head(encoder(data))
        loss = F.nll_loss(F.log_softmax(output, dim=1), y)

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        pred = torch.argmax(output, dim=1)
        total_correct += torch.eq(pred, y).sum().item()
        total_loss += loss.item() * data.shape[0]
        total_num += data.shape[0]

    return total_loss / total_num, total_correct / total_num



def source_train(args, device="cpu"):
    if args.dataset == "rotate-mnist":
        train_loader, val_loader = get_dataloader["rotate-mnist"](args.data_dir, 0, batch_size = 256, val = True)
        feat_dim = 9216
        encoder, head = TwoLayerCNN(), TwoLayerMLPHead(feat_dim, feat_dim // 2, 10)
    elif args.dataset == "portraits":
        train_loader, val_loader = get_dataloader["portraits"](args.data_dir, 0, batch_size = 256, val = True)
        feat_dim = 6272
        encoder, head = ThreeLayerCNN(), TwoLayerMLPHead(feat_dim, feat_dim // 2, 2)
    elif args.dataset == "covertype":
        train_loader, val_loader = get_dataloader["covertype"](args.data_dir, 0, batch_size = 256, val = True)
        feat_dim = 54
        encoder, head = OneLayerMLPEnc(feat_dim, feat_dim // 2, dropout_p=0.5), OneLayerMLPHead(feat_dim // 2, 7)

    encoder, head = encoder.to(device), head.to(device)
    optimizer = torch.optim.Adam(list(encoder.parameters()) + list(head.parameters()), lr=1e-3)

    best_val_acc = 0
    best_encoder, best_head = None, None
    staleness = 0
    patience = 5

    for e in range(1, args.train_epochs + 1):
        train_loss, train_acc = train(train_loader, encoder, head, optimizer, device=device)
        val_loss, val_acc = eval(val_loader, encoder, head, device=device)
        print(f"Epoch: {e} Train Loss: {round(train_loss, 3)} Train Acc: {round(train_acc, 3)} Val Loss: {round(val_loss, 3)} Val Acc: {round(val_acc, 3)}")
        if val_acc > best_val_acc:
            best_val_acc = val_acc
            best_encoder, best_head = deepcopy(encoder), deepcopy(head)
            staleness = 0
        else:
            staleness += 1

        if staleness > patience:
            break

    return best_encoder, best_head, train_loader, val_loader


def main(args):
    set_random_seeds(args.random_seed)
    device = get_device(args.gpuID)
    encoder, head, src_train_loader, src_val_loader = source_train(args, device)
    # summary(encoder)
    # summary(head)
    if args.method == "wo-adapt":
        pass
    elif args.method == "direct-adapt":
        adapter = SelfTrainer(encoder, head, device)
        domains = get_domain[args.dataset]
        tgt_train_loader = get_dataloader[args.dataset](args.data_dir, len(domains) - 1, batch_size=256, val=False)
        confidence_q_list = [0.1]
        d_name = str(len(domains) - 1)
        adapter.adapt(d_name, tgt_train_loader, confidence_q_list, args)
        encoder, head = adapter.get_encoder_head()
    elif args.method == "gradual-selftrain":
        adapter = SelfTrainer(encoder, head, device)
        domains = get_domain[args.dataset]
        confidence_q_list = [0.1]
        for domain_idx in range(1, len(domains)):
            print(f"Domain Idx: {domain_idx}")
            d_name = str(domain_idx)
            train_loader = get_dataloader[args.dataset](args.data_dir, domain_idx, batch_size=256, val=False)
            adapter.adapt(d_name, train_loader, confidence_q_list, args)
        encoder, head = adapter.get_encoder_head()
        print("PL Acc List:", adapter.pl_acc_list)
    elif args.method == "pseudo-label":
        model = Model(encoder, head).to(device)
        adapter = PseudoLabelTrainer(model, src_train_loader, src_val_loader, device)
        domains = get_domain[args.dataset]
        confidence_q_list = [0.1]
        tradeoff_list = [0.5, 1, 5]
        for domain_idx in range(1, len(domains)):
            print(f"Domain Idx: {domain_idx}")
            d_name = str(domain_idx)
            train_loader, val_loader = get_dataloader[args.dataset](args.data_dir, domain_idx, batch_size=256, val=True)
            adapter.adapt(d_name, train_loader, val_loader, confidence_q_list, tradeoff_list, args)
        model = adapter.get_model()
        encoder, head = model.get_encoder_head()
    elif args.method == "gradual-domain-ensemble":
        domains = get_domain[args.dataset]
        total_train_num = get_total_train_num[args.dataset]
        class_num = get_class_num[args.dataset]
        Z = torch.zeros(total_train_num, class_num, dtype=torch.float)
        z = torch.zeros(total_train_num, class_num, dtype=torch.float)
        domain2trainloader = OrderedDict()
        for domain_idx in range(1, len(domains)):
            print(f"Domain Idx: {domain_idx}")
            if domain_idx == len(domains) - 1:
                train_loader, val_loader = get_dataloader[args.dataset](args.data_dir, domain_idx, batch_size=256, val=True, indexed=True)
            else:
                train_loader = get_dataloader[args.dataset](args.data_dir, domain_idx, batch_size=256, val=False, indexed=True)
            domain2trainloader[domain_idx] = train_loader

        model = Model(encoder, head).to(device)
        momentum_list = [0.0, 0.1, 0.2, 0.3]
        confidence_q_list = [0.1]
        performance_dict = dict()
        for momentum in momentum_list: # global hyper-parameter, same across all domains
            # gradual adaptation
            adapter = GradualDomainEnsemble(deepcopy(model), Z, z, momentum, device)
            for domain_idx in range(1, len(domains)):
                print(f"Domain Idx: {domain_idx}")
                adapter.adapt(domain_idx, domain2trainloader, confidence_q_list, args)

            score = adapter.target_validate(val_loader)
            adapted_model = adapter.get_model()
            performance_dict[momentum] = {'model': adapted_model, 'score': score, 'pl_acc_list': adapter.pl_acc_list}
        # hyper-parameter selection
        best_score = -np.inf
        best_momentum = None
        best_model = None
        best_pl_acc_list = None
        for momentum, ckpt_dict in performance_dict.items():
            score = ckpt_dict['score']
            print(f"Momentum: {momentum} Score: {round(score, 3)}")
            if score > best_score:
                best_momentum = momentum
                best_model = ckpt_dict['model']
                best_pl_acc_list = ckpt_dict['pl_acc_list']
                best_score = score
        print(f"Best momentum: {best_momentum} Best score: {round(best_score, 3)} Best PL Acc List: {best_pl_acc_list}")
        model = best_model
        encoder, head = model.get_encoder_head()

    elif args.method == "gradual-selftrain-bagging":
        k = 3
        adapter = SelfTrainerBagging(encoder, head, k, device)
        domains = get_domain[args.dataset]
        confidence_q_list = [0.1]
        for domain_idx in range(1, len(domains)):
            d_name = str(domain_idx) + "_" + str(k)
            train_loader = get_dataloader[args.dataset](args.data_dir, domain_idx, batch_size=256, val=False)
            adapter.adapt(d_name, train_loader, confidence_q_list, args)
        encoder_list, head_list = adapter.get_encoder_head()
        encoder, head = encoder_list[0], head_list[0]
        print("PL Acc List:", adapter.pl_acc_list)

    elif args.method == "gradual-selftrain-bagging-1":
        k = 1
        adapter = SelfTrainerBagging(encoder, head, k, device)
        domains = get_domain[args.dataset]
        confidence_q_list = [0.1]
        for domain_idx in range(1, len(domains)):
            d_name = str(domain_idx) + "_" + str(k)
            train_loader = get_dataloader[args.dataset](args.data_dir, domain_idx, batch_size=256, val=False)
            adapter.adapt(d_name, train_loader, confidence_q_list, args)
        encoder_list, head_list = adapter.get_encoder_head()
        encoder, head = encoder_list[0], head_list[0]
        print("PL Acc List:", adapter.pl_acc_list)



    # save encoder, head
    os.makedirs(os.path.join(args.ckpt_dir, args.dataset), exist_ok=True)
    torch.save({"encoder": encoder.state_dict(),
                "head": head.state_dict()},
               os.path.join(args.ckpt_dir, args.dataset, f'{args.method}_{args.random_seed}.pt'))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=str, help="name of the dataset")
    parser.add_argument("--data_dir", type=str, help="path to data directory")
    parser.add_argument("--log_dir", type=str, help="path to log directory", default="runs")
    parser.add_argument("--ckpt_dir", type=str, help="path to model checkpoint directory", default="checkpoints")
    parser.add_argument("--result_dir", type=str, help="path to performance results directory", default="results")
    parser.add_argument("--method", type=str, help="adaptation method")
    parser.add_argument("--train_epochs", type=int, help="number of training epochs", default=50)
    parser.add_argument("--adapt_epochs", type=int, help="number of adaptation epochs", default=20)
    parser.add_argument("--adapt_lr", type=float, help="learning rate for adaptation optimizer", default=1e-3)
    parser.add_argument("--analyze_feat", help="whether save features", nargs='?', type=bool, const=1, default=0)
    parser.add_argument("--random_seed", type=int, help="random seed", default=42)
    parser.add_argument("--gpuID", type=int, help="which gpu to use", default=0)
    args = parser.parse_args()
    main(args)
