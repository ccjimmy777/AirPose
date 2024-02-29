#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Preprocess dataset for Synthetic data training
import pickle as pkl
import sys
import os

# data_root = sys.argv[1]
data_root = "/mnt/d/data/airpose/copenet_synthetic"  # for local use
# data_root = "/mnt/data/copenet_synthetic"  # for aliyun use

# 从 pickle 文件加载训练数据集
train_ds = pkl.load(open(os.path.join(data_root,"dataset","train_pkls.pkl"),"rb"))
test_ds = pkl.load(open(os.path.join(data_root,"dataset","test_pkls.pkl"),"rb"))

r"""
代码使用列表推导式遍历 train_ds 列表，并生成一个新的文件路径列表。
os.path.join 函数用于将 data_root 目录与每个文件路径的剩余部分连接起来。
*x.split("/")[-4:] 表达式将每个文件路径按 "/" 分隔，并选择最后四个元素，然后作为单独的参数传递给 os.path.join。
最后，生成的文件路径列表赋值给 train_ds 变量。
"""
train_ds = [os.path.join(data_root,*x.split("/")[-4:]) for x in train_ds]
test_ds = [os.path.join(data_root,*x.split("/")[-4:]) for x in test_ds]

# 将 python 对象序列化并保存到 pickle 文件
pkl.dump(train_ds,open(os.path.join(data_root,"dataset","train_pkls.pkl"),"wb"))
pkl.dump(test_ds,open(os.path.join(data_root,"dataset","test_pkls.pkl"),"wb"))

print("done!!!")