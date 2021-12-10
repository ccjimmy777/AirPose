import torch
import torch.nn as nn
import torchvision.models.resnet as resnet
import numpy as np
import math
from ..utils.geometry import rot6d_to_rotmat

class Bottleneck(nn.Module):
    """ Redefinition of Bottleneck residual block
        Adapted from the official PyTorch implementation
    """
    expansion = 4

    def __init__(self, inplanes, planes, stride=1, downsample=None):
        super(Bottleneck, self).__init__()
        self.conv1 = nn.Conv2d(inplanes, planes, kernel_size=1, bias=False)
        self.bn1 = nn.BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, stride=stride,
                               padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)
        self.conv3 = nn.Conv2d(planes, planes * 4, kernel_size=1, bias=False)
        self.bn3 = nn.BatchNorm2d(planes * 4)
        self.relu = nn.ReLU(inplace=True)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        out = self.bn3(out)

        if self.downsample is not None:
            residual = self.downsample(x)

        out += residual
        out = self.relu(out)

        return out

class copenet(nn.Module):
    """ SMPL Iterative Regressor with ResNet50 backbone
    """

    def __init__(self, block, layers, smpl_mean_params):
        self.inplanes = 64
        super(copenet, self).__init__()
        npose = 3 + 22 * 6
        self.conv1 = nn.Conv2d(3, 64, kernel_size=7, stride=2, padding=3,
                               bias=False)
        self.bn1 = nn.BatchNorm2d(64)
        self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)
        self.layer1 = self._make_layer(block, 64, layers[0])
        self.layer2 = self._make_layer(block, 128, layers[1], stride=2)
        self.layer3 = self._make_layer(block, 256, layers[2], stride=2)
        self.layer4 = self._make_layer(block, 512, layers[3], stride=2)
        self.avgpool = nn.AvgPool2d(7, stride=1)
        self.fc1 = nn.Linear(512 * block.expansion + npose + 10 + 3, 1024)
        self.drop1 = nn.Dropout()
        self.fc2 = nn.Linear(1024, 1024)
        self.drop2 = nn.Dropout()
        self.decpose = nn.Linear(1024, npose)
        self.decshape = nn.Linear(1024, 10)
        self.deccam = nn.Linear(1024, 3)
        nn.init.xavier_uniform_(self.decpose.weight, gain=0.01)
        nn.init.xavier_uniform_(self.decshape.weight, gain=0.01)
        nn.init.xavier_uniform_(self.deccam.weight, gain=0.01)

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, math.sqrt(2. / n))
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()

        mean_params = np.load(smpl_mean_params)
        init_pose = torch.from_numpy(mean_params['pose'][:]).unsqueeze(0)
        init_shape = torch.from_numpy(mean_params['shape'][:].astype('float32')).unsqueeze(0)
        init_position = torch.from_numpy(np.array([0.0,0.0,10.0/0.05])).unsqueeze(0).float()
        self.register_buffer('init_pose', init_pose)
        self.register_buffer('init_shape', init_shape)
        self.register_buffer('init_position', init_position)


    def _make_layer(self, block, planes, blocks, stride=1):
        downsample = None
        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                nn.Conv2d(self.inplanes, planes * block.expansion,
                          kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(planes * block.expansion),
            )

        layers = []
        layers.append(block(self.inplanes, planes, stride, downsample))
        self.inplanes = planes * block.expansion
        for i in range(1, blocks):
            layers.append(block(self.inplanes, planes))

        return nn.Sequential(*layers)

    def forward(self, x, bb, init_position, init_cam=None, init_theta=None, init_shape=None, iters = 3):
        batch_size = x.shape[0]

        # if init_position is None:
        #     init_position = self.init_position.expand(batch_size, -1)

        if init_theta is None:
            init_pose = torch.cat([init_position, self.init_pose[:,:22*6].expand(batch_size, -1)],axis=1)
        else:
            init_pose = torch.cat([init_position, init_theta],axis=1)
        if init_shape is None:
            init_shape = self.init_shape.expand(batch_size, -1)
        
        
         # Feed images in the network to predict camera and SMPL parameters 
        xf = self.forward_feat_ext(x)

        pred_pose, pred_betas = self.forward_reg(xf,
                                                            bb, 
                                                            init_pose,
                                                            init_shape)

        for it in range(int(iters)-1):
            pred_pose, pred_betas = self.forward_reg(xf,
                                                                bb,
                                                                pred_pose,
                                                                pred_betas)

        return pred_pose, pred_betas

    def forward_feat_ext(self, x):

        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        x1 = self.layer1(x)
        x2 = self.layer2(x1)
        x3 = self.layer3(x2)
        x4 = self.layer4(x3)

        xf = self.avgpool(x4)
        xf = xf.view(xf.size(0), -1)

        return xf
    
    def forward_reg(self, xf, bb, pred_pose, pred_shape):
        
        xc = torch.cat([xf, bb, pred_pose, pred_shape],1)
        xc = self.fc1(xc)
        xc = self.drop1(xc)
        xc = self.fc2(xc)
        xc = self.drop2(xc)
        
        pred_pose = self.decpose(xc) + pred_pose
        pred_shape = self.decshape(xc) + pred_shape

        return pred_pose, pred_shape
        
        # pred_rotmat = rot6d_to_rotmat(pred_pose).view(batch_size, 24, 3, 3)

    def create_ftl_mat(self, batch_size, extr_inv, bb):
        mat = torch.zeros((batch_size, 15,15),device=extr_inv.device).float()
        mat[:,:3,:3] = extr_inv[:,:3,:3]
        mat[:,3:5,3:5] = self.create_ftl_mat_single(extr_inv[:,0,3],20,-20)
        mat[:,5:7,5:7] = self.create_ftl_mat_single(extr_inv[:,1,3],20,-20)
        mat[:,7:9,7:9] = self.create_ftl_mat_single(extr_inv[:,2,3],20,-20)
        mat[:,9:11,9:11] = self.create_ftl_mat_single(bb[:,0],1,-1)
        mat[:,11:13,11:13] = self.create_ftl_mat_single(bb[:,1],1,-1)
        #####################################################
        mat[:,13:15,13:15] = self.create_ftl_mat_single(bb[:,2],0.1,2)
        #####################################################
        return mat

    def create_ftl_mat_single(self, t, t_max, t_min):
        ct = (t - 0.5*(t_max + t_min))/(0.5*(t_max - t_min))
        st = torch.sqrt(1-ct**2)
        col0 = torch.cat([ct.unsqueeze(1), -st.unsqueeze(1)],dim=1)
        col1 = torch.cat([st.unsqueeze(1), ct.unsqueeze(1)],dim=1)
        mat = torch.cat([col0.unsqueeze(2),col1.unsqueeze(2)], dim=2)
        return mat

def getcopenet(smpl_mean_params, pretrained=True, **kwargs):
    """ Constructs an HMR model with ResNet50 backbone.
    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
    """
    model = copenet(Bottleneck, [3, 4, 6, 3],  smpl_mean_params, **kwargs)
    if pretrained:
        resnet_imagenet = resnet.resnet50(pretrained=True)
        model.load_state_dict(resnet_imagenet.state_dict(),strict=False)
    return model