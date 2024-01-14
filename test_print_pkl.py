# -*- coding: utf-8 -*-

import argparse
import pickle
import numpy as np

# 创建命令行参数解析器
parser = argparse.ArgumentParser(description='Read and view contents of a large pickle file.')
# 添加-p/--path参数，用于指定pkl文件的路径
parser.add_argument('-p', '--path', type=str, help='Path of the pickle file')

# 解析命令行参数
args = parser.parse_args()

# 检查是否提供了pkl文件的路径
if not args.path:
    print('Please provide the path of the pickle file using -p/--path argument.')
else:
    # 打开.pkl文件
    with open(args.path, 'rb') as file:
        # 创建一个循环，逐块读取文件内容
        while True:
            try:
                # 使用pickle模块的load()函数逐块加载文件内容
                data = pickle.load(file)

                if isinstance(data, dict):
                    for key, value in data.items():
                        print("Key: {}".format(key))
                        print("Value Type: {}".format(type(value)))
                        # print(f"Value: {value}")
                        if isinstance(value, np.ndarray):
                            print("Value is an np.array")
                            print("Dimension: {}".format(value.ndim))
                            print("Shape: {}".format(value.shape))
                            print("Element Type: {}".format(value.dtype))
                        elif isinstance(value, list):
                            print("Value is a list")
                            print("Length: {}".format(len(value)))
                            print("Element Type: {}".format(type(value[0])))
                            print("Content:")
                            for element in value:
                                print(element)
                        elif isinstance(value, dict):
                            for key, value in value.items():
                                print("---Key: {}".format(key))
                                print("---Value Type: {}".format(type(value)))
                                # print(f"Value: {value}")
                                if isinstance(value, np.ndarray):
                                    print("------Value's Value is an np.array")
                                    print("------Value's Dimension: {}".format(value.ndim))
                                    print("------Value's Shape: {}".format(value.shape))
                                    print("------Value's Element Type: {}".format(value.dtype))
                        else:
                            print(f"Value: {value}")
                        print("\n")
                else:
                    print("The contents of the pkl file are not in dictionary format.")
                
                # 查看文件内容的一部分
                # print(data)
            
                # 如果您只想查看文件的前几个块，可以在此添加一个计数器，并在达到某个条件时终止循环
                # count += 1
                # if count >= num_blocks:
                #     break
            
            except EOFError:
                # 如果读取到文件末尾，退出循环
                break