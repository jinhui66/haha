"""
PoolFormer implementation
"""
import os
os.environ['CUDA_VISIBLE_DEVICES'] = '2'
import copy
import torch
import torch.nn as nn
from torch.nn.modules.batchnorm import _BatchNorm
from timm.data import IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD
from Config import parse_args
from timm.models.layers import DropPath, trunc_normal_
from datetime import datetime
from pathlib import Path
from typing import Sequence
from functools import partial, reduce
import torchvision
import time
from torch.utils.data import DataLoader, Dataset, Subset
from torchvision import transforms
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm
from Dataset import DoubleEyesDataset, DoubleTransformedSubset
from utils.metrics import Metric_Manager_Normal
from sklearn.model_selection import StratifiedKFold, KFold
from model.DoublePretrained import DoubleImageModel7Class
import numpy as np
from model.DoubleResnet import DoubleResNet


def to0_1(tensor):
    bool_tensor = tensor > 0.5
    # 将布尔型张量转换为浮点型张量，True变为1.0，False变为0.0
    result_tensor = bool_tensor.float()
    return result_tensor

def k_fold_cross_validation(device, eyes_dataset, epochs, k_fold=5,
                            batch_size=8, workers=2, print_freq=1, checkpoint_dir="./result",
                            best_result_model_path="model", total_transform=None):
    start = time.time()
    # skf = StratifiedKFold(n_splits=k_fold, shuffle=True, random_state=42)
    # best_fold_metrics = []
    best_acc_overall = 0.
    epoch_acc_dict = {}
    
    train_metric = Metric_Manager_Normal(num_classes=8)
    valid_metric = Metric_Manager_Normal(num_classes=8)
    
    # 获取数据集的标签
    # labels = [data[1] for data in eyes_dataset]  # 假设dataset[i]的第2项是label
    kf = KFold(n_splits=k_fold, shuffle=True, random_state=0)  # init KFold
    # Iterations = 1
    
    for fold, (train_index, test_index) in enumerate(kf.split(eyes_dataset)):  # split
    # for fold, (train_idx, val_idx) in enumerate(skf.split(eyes_dataset, labels), 1):
        # get train, val
        k_train_fold = Subset(eyes_dataset, train_index)
        k_test_fold = Subset(eyes_dataset, test_index)
        # 应用转换
        train_dataset = DoubleTransformedSubset(k_train_fold, transform=total_transform['train_transforms'])
        val_dataset = DoubleTransformedSubset(k_test_fold, transform=total_transform['validation_transforms'])

        # package type of DataLoader
        train_dataloader = torch.utils.data.DataLoader(dataset=train_dataset, batch_size=batch_size, shuffle=True)
        eval_dataloader = torch.utils.data.DataLoader(dataset=val_dataset, batch_size=batch_size, shuffle=False)
        
        X = torch.zeros(7)
        Count = 0
        for (_, _, labels) in train_dataloader:
            X += torch.sum(labels[:, 1:],dim=0)
            Count += labels.shape[0]
            # break
            # print(X, Count)
        # X = torch.tensor([100., 100., 100., 100., 100., 100., 100.])
        print("各疾病总数:", X, Count)
        X = Count / X - 1
        print("各疾病损失权重:", X)
        # X = torch.tensor([ 4.7388, 12.5706, 12.4128, 17.0234, 33.4328, 14.9103,  2.0760])
        loss_fn = nn.BCEWithLogitsLoss(pos_weight=X).to(device)  # 损失函数

        model = DoubleImageModel7Class(num_classes=7, pretrained_path=args.pretrainedModelPath).to(device)
        
        
        optimizer = torch.optim.Adam(model.parameters(), lr=args.learning_rate, weight_decay=1e-7, betas=(0.9, 0.98))
        total_params = sum(p.numel() for p in model.parameters())
        print('总参数个数:{}'.format(total_params))
        best_score = 0.
        best_epoch = 0
        best_acc = 0.
        best_metrics = {}
        for e in range(1, epochs + 1):
            train_metric.reset()
            valid_metric.reset()
            
            model.train()
            total_train_loss = 0.0
            total_eval_loss = 0.0
            train_iterator = tqdm(train_dataloader, desc=f"Training Epoch {e}", unit="batch")
            for batch_idx, (left_images, right_images, labels) in enumerate(train_iterator):
                left_images, right_images, labels = left_images.to(device), right_images.to(device), labels.to(device)

                optimizer.zero_grad()

                predictions = model(left_images, right_images)

                loss = loss_fn(predictions, labels[:, 1:])
                loss.backward()
                optimizer.step()
                total_train_loss += loss.item()
                
                predictions = to0_1(predictions)
                

                train_metric.update(predictions, labels)
                # print(train_metric.get_metrics())

            train_accuracy, train_recall, train_precision, train_specificity = train_metric.get_metrics()
            score = train_metric.compute_score()
            # total_score = score[0]
            total_train_loss /= len(train_index)

            print(f"Epoch [{e}/{epochs}]: train_loss={total_train_loss:.3f}, accuracy={train_accuracy}, recall={train_recall}, precision={train_precision}, specificity={train_specificity}, train_score={score}")
            if e % print_freq == 0:
                output_result = f"Epoch [{e}/{epochs}]: \n train_loss={total_train_loss:.3f}, \n accuracy={train_accuracy}, \n recall={train_recall}, \n precision={train_precision}, \n specificity={train_specificity}, \n train_score:{score}"

                # 将完整的字符串写入文件
                with open(otuput_file, 'a') as f:
                    f.write(output_result + '\n')  # 在最后添加一个换行符以保持格式整洁
            
            with torch.no_grad():
                model.eval()
                
                eval_iterator = tqdm(eval_dataloader, desc=f"Evaluating Epoch {e}", unit="batch")
                for batch_idx, (left_images, right_images, labels) in enumerate(eval_iterator):
                    left_images, right_images, labels = left_images.to(device), right_images.to(device), labels.to(device)

                    predictions = model(left_images, right_images)
                    
                    # predictions = torch.sigmoid(predictions)
                    loss = loss_fn(predictions, labels[:, 1:])
                    total_eval_loss += loss.item()
                    
                    # print(predictions[:, 0], labels[:, 0])
                    predictions = to0_1(predictions)
                    
                    # _, val_predicted = torch.max(predictions.data, 1)

                    valid_metric.update(predictions, labels)
                    
                valid_accuracy, valid_recall, valid_precision, valid_specificity = valid_metric.get_metrics()
                score = valid_metric.compute_score()
                total_score = score[0]

                total_eval_loss /= len(test_index)

            if total_score > best_score:
                best_score = total_score
                best_epoch = e
                torch.save(model.state_dict(), f'./checkpoint/pretrained{fold}.pth')

                
            print(f"Epoch [{e}/{epochs}]: val_loss={total_eval_loss:.3f}, accuracy={valid_accuracy}, recall={valid_recall}, precision={valid_precision}, specificity={valid_specificity}, val_score={score}")
            
            if e % print_freq == 0:
                output_result = f"Epoch [{e}/{epochs}]: \n val_loss={total_eval_loss:.3f}, \n accuracy={valid_accuracy}, \n recall={valid_recall}, \n precision={valid_precision}, \n specificity={valid_specificity}, \n val_score={score} \n"

                # 将完整的字符串写入文件
                with open(otuput_file, 'a') as f:
                    f.write(output_result + '\n')  # 在最后添加一个换行符以保持格式整洁



        # best_fold_metrics.append(best_metrics)
        print(f"Fold {fold} Best Epoch: {best_epoch}, Best Val Acc: {best_acc:.3f}")
        with open(otuput_file, 'a') as f:
            f.write(f"Fold {fold} Best Epoch: {best_epoch}, Best Val Acc: {best_acc:.3f} \n")  # 在最后添加一个换行符以保持格式整洁
        epoch_acc_dict[fold] = best_acc

        # if best_acc > best_acc_overall:
        #     best_acc_overall = best_acc
        # break

    end = time.time()
    print(f"Cross-validation result:{epoch_acc_dict}")
    print(f"Total training time: {(end - start) // 60}m {(end - start) % 60}s")
    



if __name__ == '__main__':
    current_time = "{0:%Y%m%d_%H_%M}".format(datetime.now())
    args = parse_args()
    # 获取运行的设备
    if args.device != 'cpu':
        device = torch.device(args.device if torch.cuda.is_available() else "cpu")
    else:
        device = torch.device("cpu")

    # Define transforms for each dataset separately
    mean = [0.5,0.5,0.5]
    std = [0.5,0.5,0.5]
    image_size = args.Image_size

    train_validation_test_transform={
        'train_transforms':transforms.Compose([
        transforms.Resize((image_size, image_size)),
        transforms.RandomHorizontalFlip(0.5),
        transforms.RandomRotation(45),
        transforms.RandomAdjustSharpness(1.3, 1),
        transforms.ToTensor(),
        transforms.Normalize(mean, std)
        ]),
        'validation_transforms':transforms.Compose([
        transforms.Resize((image_size, image_size)),
        transforms.ToTensor(),
        transforms.Normalize(mean, std)
        ]),
        'test_transforms':transforms.Compose([
        transforms.Resize((image_size, image_size)),
        transforms.ToTensor(),
        transforms.Normalize(mean, std)
        ])
    }
    
    data_dir = "/data3/wangchangmiao/jinhui/eye/Enhanced"
    # 初始化自定义数据集
    dataset = DoubleEyesDataset(csv_file="./data/double_valid_data.csv",
                            img_prefix=data_dir,
                            transform=None)

    # 定义模型保存的文件夹
    model_dir = args.checkpoint_dir
    Path(model_dir).mkdir(parents=True, exist_ok=True)
    # 训练的总轮数
    EPOCH = 100
    epoch = 0
    otuput_file = "double_pretrained.txt"
    k_fold_cross_validation(device, dataset, EPOCH, k_fold=args.k_split_value,
                            batch_size=args.batch_size, workers=2, print_freq=1, checkpoint_dir=model_dir,
                            best_result_model_path="model", total_transform=train_validation_test_transform)