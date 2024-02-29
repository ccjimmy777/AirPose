"""
This file defines the core research contribution   
"""
import os
import time
import sys
import torch
import torch.nn as nn
from torch.nn import functional as F
from torch.utils.data import DataLoader
from torchvision.datasets import MNIST
import torchvision.transforms as transforms
from argparse import ArgumentParser
import numpy as np
import torchgeometry as tgm

import copy
from .models import model_copenet_singleview
from .dsets import aerialpeople
import cv2
import torchvision
from .smplx.smplx import SMPLX, lbs
import pickle as pk
from .utils.renderer import Renderer
from . import constants as CONSTANTS
from .utils.utils import transform_smpl, add_noise_input_cams,add_noise_input_smpltrans
from .utils.geometry import batch_rodrigues, perspective_projection, estimate_translation, rot6d_to_rotmat

import pytorch_lightning as pl

from .config import device

smplx = None
smplx_test = None

def create_smplx(copenet_home,train_batch_size,val_batch_size):
    global smplx 
    smplx = SMPLX(os.path.join(copenet_home,"src/copenet/data/smplx/models/smplx"),
                         batch_size=train_batch_size,
                         create_transl=False)

    global smplx_test 
    smplx_test = SMPLX(os.path.join(copenet_home,"src/copenet/data/smplx/models/smplx"),
                         batch_size=train_batch_size,
                         create_transl=False)

class copenet_singleview(pl.LightningModule):

    def __init__(self, hparams):
        super(copenet_singleview, self).__init__()
        # not the best model...
        self.save_hyperparameters(hparams)
        self.model = model_copenet_singleview.getcopenet(os.path.join(self.hparams.copenet_home,"src/copenet/data/smpl_mean_params.npz"))

        create_smplx(self.hparams.copenet_home,self.hparams.batch_size,self.hparams.val_batch_size)
        
        smplx.to(device)
        smplx_test.to(device)
        
        smplx_hand_idx = pk.load(open(os.path.join(self.hparams.copenet_home,"src/copenet/data/smplx/MANO_SMPLX_vertex_ids.pkl"),'rb'))
        smplx_face_idx = np.load(os.path.join(self.hparams.copenet_home,"src/copenet/data/smplx/SMPL-X__FLAME_vertex_ids.npy"))
        self.register_buffer("body_only_mask",torch.ones(smplx.v_template.shape[0],1))
        self.body_only_mask[smplx_hand_idx['left_hand'],:] = 0
        self.body_only_mask[smplx_hand_idx['right_hand'],:] = 0
        self.body_only_mask[smplx_face_idx,:] = 0
        
        self.mseloss = nn.MSELoss(reduction='none')

        self.focal_length = CONSTANTS.FOCAL_LENGTH
        self.renderer = Renderer(focal_length=self.focal_length, img_res=CONSTANTS.IMG_SIZE, faces=smplx.faces)

    def forward(self, **kwargs):
        return self.model(**kwargs)
        

    def get_loss(self,input_batch, pred_smpltrans, pred_rotmat, pred_betas, pred_output_cam, pred_joints_2d_cam):

        gt_smplpose_rotmat = input_batch['smplpose_rotmat'] # SMPL pose rotation matrices
        gt_smpltrans_rel = input_batch['smpltrans_rel0'] # SMPL trans parameters
        gt_smplorient_rel = input_batch['smplorient_rel0'] # SMPL orientation parameters
        gt_vertices = input_batch['smpl_vertices'].squeeze(1)
        gt_joints = input_batch['smpl_joints'].squeeze(1)
        gt_joints_2d_cam = input_batch['smpl_joints_2d0'].squeeze(1)
        
        loss_keypoints = (self.mseloss(pred_joints_2d_cam[:,:22], gt_joints_2d_cam[:,:22])).mean()

    
        loss = self.mseloss(pred_output_cam.joints[:,:22], gt_joints[:,:22])
        loss[:,[4,5,18,19]] *= self.hparams.limbs3d_loss_weight
        loss[:,[7,8,20,21]] *= self.hparams.limbs3d_loss_weight**2
        loss_keypoints_3d = loss.mean()

        loss_regr_shape = self.mseloss(pred_output_cam.vertices, gt_vertices).mean()

        loss_regr_trans = self.mseloss(pred_smpltrans, gt_smpltrans_rel).mean()
        
        loss_rootrot = self.mseloss(pred_rotmat[:,:1], gt_smplorient_rel).mean()

        loss_rotmat = self.mseloss(pred_rotmat[:,1:], gt_smplpose_rotmat)
        # one index less than actual limbs because the root joint is not there in rotmats
        loss_rotmat[:,[3,4,17,18]] *= self.hparams.limbstheta_loss_weight
        loss_rotmat[:,[6,7,19,20]] *= self.hparams.limbstheta_loss_weight**2
        loss_regr_pose =  loss_rotmat.mean()
        
        loss_regul_betas = torch.mul(pred_betas,pred_betas).mean()

        depth_aware_z, depth_aware_z_std = self.get_depth_aware(input_batch, pred_smpltrans, pred_rotmat, pred_output_cam)
        loss_depth_aware = (self.mseloss(pred_smpltrans[:,2], depth_aware_z)/(depth_aware_z_std**2)).mean()
        
        # Compute total loss
        loss = self.hparams.trans_loss_weight * loss_regr_trans + \
                self.hparams.keypoint2d_loss_weight * loss_keypoints + \
                self.hparams.keypoint3d_loss_weight * loss_keypoints_3d + \
                self.hparams.shape_loss_weight * loss_regr_shape + \
                self.hparams.rootrot_loss_weight * loss_rootrot + \
                self.hparams.pose_loss_weight * loss_regr_pose + \
                self.hparams.beta_loss_weight * loss_regul_betas + \
                10.0 * loss_depth_aware

        loss *= 60

        losses = {'loss': loss.detach().item(),
                  'loss_regr_trans': loss_regr_trans.detach().item(),
                  'loss_keypoints': loss_keypoints.detach().item(),
                  'loss_keypoints_3d': loss_keypoints_3d.detach().item(),
                  'loss_regr_shape': loss_regr_shape.detach().item(),
                  'loss_rootrot': loss_rootrot.detach().item(),
                  'loss_regr_pose': loss_regr_pose.detach().item(),
                  'loss_regul_betas': loss_regul_betas.detach().item(),
                  'loss_depth_aware': loss_depth_aware.detach().item()}

        # print(losses)

        return loss, losses


    def fwd_pass_and_loss(self,input_batch,is_val=False,is_test=False):
        """
        Perform a forward pass through the model and calculate the loss.

        Args:
            self (object): The instance of the class.
            input_batch (dict): The input batch containing the following keys:
                - 'im0' (torch.Tensor): The input image.
                - 'smpltrans_rel0' (torch.Tensor): The SMPL trans parameters.
                - 'bb0' (torch.Tensor): The bounding box.
                - 'intr0' (torch.Tensor): The intrinsic parameters of the camera.
            is_val (bool, optional): Whether the function is being called during validation. Defaults to False.
            is_test (bool, optional): Whether the function is being called during testing. Defaults to False.

        Returns:
            tuple: A tuple containing the following elements:
                - output (dict): A dictionary containing the following keys:
                    - 'pred_vertices_cam' (torch.Tensor): The predicted vertices in camera coordinates.
                    - 'pred_j3d_cam' (torch.Tensor): The predicted 3D joints in camera coordinates.
                    - 'pred_vertices_cam_in' (torch.Tensor): The predicted vertices in camera coordinates for the input image.
                    - 'pred_angles' (torch.Tensor): The predicted angles.
                    - 'pred_betas' (torch.Tensor): The predicted betas.
                    - 'pred_smpltrans' (torch.Tensor): The predicted SMPL trans parameters.
                    - 'in_smpltrans' (torch.Tensor): The input SMPL trans parameters.
                    - 'gt_angles' (torch.Tensor): The ground truth angles.
                    - 'gt_smpltrans' (torch.Tensor): The ground truth SMPL trans parameters.
                    - 'smplorient_rel0' (torch.Tensor): The SMPL orientation parameters.
                    - 'smplpose_rotmat' (torch.Tensor): The SMPL pose rotation matrix.
                - losses (dict): A dictionary containing the loss values.
                - loss (torch.Tensor): The total loss.
        """
        
        with torch.no_grad():
            # Get data from the batch
            im = input_batch['im0'].float() # input image
            gt_smpltrans_rel = input_batch['smpltrans_rel0'] # SMPL trans parameters
            bb = input_batch['bb0']
            intr = input_batch['intr0']
            
        
            batch_size = im.shape[0]
            
            if is_test and self.hparams.testdata.lower() == "aircapdata":
                in_smpltrans = gt_smpltrans_rel
            elif self.hparams.smpltrans_noise_sigma is None:
                in_smpltrans = torch.from_numpy(np.array([0,0,10])).float().expand(batch_size, -1).clone().type_as(gt_smpltrans_rel)
            else:
                in_smpltrans, _ = add_noise_input_smpltrans(gt_smpltrans_rel,self.hparams.smpltrans_noise_sigma)

            # noisy input pose
            # gt_theta = torch.cat([gt_smplorient_rel,gt_smplpose_rotmat],dim=1)[:,:,:,:2] 
            # init_theta = gt_theta + self.hparams.theta_noise_sigma * torch.randn(batch_size,22,3,2).type_as(gt_smplorient_rel)
            # init_theta = init_theta.reshape(batch_size,22*6)

            # distance scaling
            distance_scaling = True
            if distance_scaling:
                trans_scale = 0.05
                in_smpltrans *= trans_scale
        
        pred_pose, pred_betas = self.model.forward(x = im,
                                                    bb = bb,
                                                    init_position=in_smpltrans,
                                                    iters = self.hparams.reg_iters)
                                                                        

        pred_smpltrans = pred_pose[:,:3]
        if distance_scaling:
            pred_smpltrans /= trans_scale
            in_smpltrans /= trans_scale
        pred_rotmat = rot6d_to_rotmat(pred_pose[:,3:]).view(batch_size, 22, 3, 3)

        # # Sanity check
        # #################################
        # pred_smpltrans = gt_smpltrans_rel
        # pred_rotmat = torch.cat([gt_smplorient_rel,gt_smplpose_rotmat],dim=1).view(batch_size,22,3,3)
        # #################################

        # import ipdb; ipdb.set_trace()
        if is_val or is_test:
            pred_output_cam = smplx_test.forward(betas=pred_betas, 
                                    body_pose=pred_rotmat[:,1:],
                                    global_orient=torch.eye(3,device=self.device).float().unsqueeze(0).repeat(batch_size,1,1).unsqueeze(1),
                                    transl = torch.zeros(batch_size,3).float().type_as(pred_betas),
                                    pose2rot=False)
            transf_mat = torch.cat([pred_rotmat[:,:1].squeeze(1),
                                pred_smpltrans.unsqueeze(2)],dim=2)
            pred_vertices_cam,pred_joints_cam,_,_ = transform_smpl(transf_mat,
                                                pred_output_cam.vertices.squeeze(1),
                                                pred_output_cam.joints.squeeze(1))
            if is_test:
                pred_output_cam_in = smplx_test.forward(betas=torch.zeros(batch_size,10).float().type_as(pred_betas), 
                                        body_pose=pred_rotmat[:,1:],
                                        global_orient=pred_rotmat[:,:1],
                                        transl = torch.zeros(batch_size,3).float().type_as(pred_betas),
                                        pose2rot=False)
                transf_mat_in = torch.cat([torch.eye(3,device=self.device).float().unsqueeze(0).repeat(batch_size,1,1),
                                    in_smpltrans.unsqueeze(2)],dim=2)
                pred_vertices_cam_in,_,_,_ = transform_smpl(transf_mat_in,
                                                    pred_output_cam_in.vertices.squeeze(1),
                                                    pred_output_cam_in.joints.squeeze(1))
        else:
            pred_output_cam = smplx.forward(betas=pred_betas, 
                                body_pose=pred_rotmat[:,1:],
                                global_orient=torch.eye(3,device=self.device).float().unsqueeze(0).repeat(batch_size,1,1).unsqueeze(1),
                                transl = torch.zeros(batch_size,3).float().type_as(pred_betas),
                                pose2rot=False)

            transf_mat = torch.cat([pred_rotmat[:,:1].squeeze(1),
                                pred_smpltrans.unsqueeze(2)],dim=2)

            pred_vertices_cam,pred_joints_cam,_,_ = transform_smpl(transf_mat,
                                                pred_output_cam.vertices.squeeze(1),
                                                pred_output_cam.joints.squeeze(1))
        
        pred_joints_2d_cam = perspective_projection(pred_joints_cam,
                                                   rotation=torch.eye(3).float().unsqueeze(0).repeat(batch_size,1,1).type_as(pred_betas),
                                                   translation=torch.zeros(batch_size, 3).type_as(pred_betas),
                                                   focal_length=self.focal_length,
                                                   camera_center=intr[:,:2,2].unsqueeze(0))
        
        
        if is_test:
            loss, losses = None, None
            # Pack output arguments for tensorboard logging
            pred_angles = tgm.rotation_matrix_to_angle_axis(torch.cat([pred_rotmat,torch.zeros(batch_size,22,3,1).float().type_as(pred_betas)],dim=3).view(-1,3,4)).view(batch_size,22,3)
            gt_angles = tgm.rotation_matrix_to_angle_axis(torch.cat([torch.cat([input_batch['smplorient_rel0'],input_batch['smplpose_rotmat']],dim=1),torch.zeros(batch_size,22,3,1).float().type_as(pred_betas)],dim=3).view(-1,3,4)).view(batch_size,22,3)

            output = {'pred_vertices_cam': pred_vertices_cam.detach().cpu(),
                        'pred_j3d_cam': pred_vertices_cam.detach().cpu(),
                        "pred_vertices_cam_in": pred_vertices_cam_in.detach().cpu(),
                        "pred_angles": pred_angles.detach().cpu(),
                        "pred_betas": pred_betas.detach().cpu(),
                        'pred_smpltrans': pred_smpltrans.detach().cpu(),
                        'in_smpltrans': in_smpltrans.detach().cpu(),
                        "gt_angles": gt_angles.detach().cpu(),
                        'gt_smpltrans': gt_smpltrans_rel.detach().cpu(),
                        'smplorient_rel0': input_batch['smplorient_rel0'].detach().cpu(),
                        'smplpose_rotmat': input_batch['smplpose_rotmat'].detach().cpu()}
        else:
            loss, losses = self.get_loss(input_batch,
                                pred_smpltrans,
                                pred_rotmat,
                                pred_betas,
                                pred_output_cam,
                                pred_joints_2d_cam)
            # print(loss)
            # Pack output arguments for tensorboard logging
            output = {'pred_vertices_cam': pred_vertices_cam.detach(),
                        'pred_smpltrans': pred_smpltrans.detach(),
                        'in_smpltrans': in_smpltrans.detach(),
                        'gt_smpltrans': gt_smpltrans_rel.detach()}
        

        return output, losses, loss


    def training_step(self, batch, batch_idx):
        """
        Performs a single training step on the given batch of data.

        Args:
            batch: The input batch of data.
            batch_idx: The index of the current batch.

        Returns:
            A dictionary containing the loss value.
        """
        
        output, losses, loss = self.fwd_pass_and_loss(batch,is_val=False, is_test=False)

        with torch.no_grad():
        # logging
            if batch_idx % self.hparams.summary_steps == 0:
                train_summ_pred_image, train_summ_in_image = self.summaries(batch, output, losses, is_test=False)
                self.logger.experiment.add_image('train_pred_shape_cam', train_summ_pred_image, self.global_step)
                self.logger.experiment.add_image('train_input_images',train_summ_in_image, self.global_step)
                for loss_name, val in losses.items():
                    self.logger.experiment.add_scalar(loss_name + '/train', val, self.global_step)

        return {'losses': losses, "loss" : loss}
    
    def training_epoch_end(self, outputs):
        #  the function is called after every epoch is completed
        with torch.no_grad():
            # logging
            for loss_name, _ in outputs[0]["losses"].items():
                mean_val = torch.stack([x['losses'][loss_name] for x in outputs]).mean()
                self.logger.experiment.add_scalar(loss_name + '/train', mean_val, self.current_epoch)

            # calculating average loss  
            avg_loss = torch.stack([x['loss'] for x in outputs]).mean()
            self.logger.experiment.add_scalar("avg_loss" + '/train', avg_loss, self.current_epoch)
        
        return {"loss": avg_loss}

    def validation_step(self, batch, batch_idx):
        # OPTIONAL
        with torch.no_grad():
            output, losses, loss = self.fwd_pass_and_loss(batch,is_val=True, is_test=False)

            if batch_idx % self.hparams.val_summary_steps == 0:
                val_summ_pred_image, val_summ_in_image = self.summaries(batch, output, losses, is_test=False)

                # logging
                self.logger.experiment.add_image('val_pred_shape_cam', val_summ_pred_image, self.global_step)
                self.logger.experiment.add_image('val_input_images',val_summ_in_image, self.global_step)
        
        return {'val_losses': losses,"val_loss":loss}

    def validation_epoch_end(self, outputs):
        with torch.no_grad():
            for loss_name, _ in outputs[0]["val_losses"].items():
                mean_val = torch.stack([x['val_losses'][loss_name] for x in outputs]).mean()
                self.logger.experiment.add_scalar(loss_name + '/val', mean_val, self.current_epoch)

            # self.log("val_loss", np.mean([x["val_loss"].cpu().numpy() for x in outputs]))
            avg_loss = torch.stack([x["val_loss"] for x in outputs]).mean()
            self.logger.experiment.add_scalar("avg_loss" + '/val', avg_loss, self.current_epoch)

        return {"val_loss": avg_loss}

    def configure_optimizers(self):
        # REQUIRED
        # can return multiple optimizers and learning_rate schedulers
        optimizer = torch.optim.Adam(self.model.parameters(), 
                                lr=self.hparams.lr,
                                weight_decay=0,
                                amsgrad=True)
        # scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer)

        return optimizer#, [scheduler]

    def train_dataloader(self):
        # REQUIRED
        train_dset, _ = aerialpeople.get_aerialpeople_seqsplit(self.hparams.datapath)
        return DataLoader(train_dset, batch_size=self.hparams.batch_size,
                            num_workers=self.hparams.num_workers,
                            pin_memory=self.hparams.pin_memory,
                            shuffle=self.hparams.shuffle_train,
                            drop_last=True)

    def val_dataloader(self):
        # OPTIONAL
        _, val_dset = aerialpeople.get_aerialpeople_seqsplit(self.hparams.datapath)
        return DataLoader(val_dset, batch_size=self.hparams.val_batch_size,
                            num_workers=self.hparams.num_workers,
                            pin_memory=self.hparams.pin_memory,
                            shuffle=False,  # old: self.hparams.shuffle_train
                            drop_last=True)

    def summaries(self, input_batch,output, losses, is_test):
        batch_size = input_batch['im0'].shape[0]
        skip_factor = 4    # number of samples to be logged
        img_downsize_factor = 5
        

        with torch.no_grad():
            im = input_batch['im0'][::int(batch_size/skip_factor)] * torch.tensor([0.229, 0.224, 0.225], device=self.device).reshape(1,3,1,1)
            im = im + torch.tensor([0.485, 0.456, 0.406], device=self.device).reshape(1,3,1,1)
            images = []
            for i in range(0,batch_size,int(batch_size/skip_factor)):
                blank_img = torch.zeros(3,1080,1920).float()
                blank_img[:,input_batch["crop_info0"][i,0,0]:input_batch["crop_info0"][i,1,0],
                    input_batch["crop_info0"][i,0,1]:input_batch["crop_info0"][i,1,1]] = torch.from_numpy(cv2.imread(input_batch['im0_path'][i])[:,:,::-1]/255.).float().permute(2,0,1)
                images.append(blank_img)
            images = torch.stack(images)
            pred_vertices_cam = output['pred_vertices_cam'][::int(batch_size/skip_factor)]
            # pred_vertices_cam = input_batch['smpl_vertices_rel'][::int(batch_size/skip_factor)].squeeze(1)
            
            images_pred_cam = self.renderer.visualize_tb(pred_vertices_cam,
                                                        torch.zeros(batch_size,3,device=self._device).float(),
                                                        torch.eye(3,device=self._device).float().unsqueeze(0).repeat(batch_size,1,1),
                                                        images)
            
            summ_pred_image = images_pred_cam[:,::img_downsize_factor,::img_downsize_factor]
            
            summ_in_image = torchvision.utils.make_grid(im)
            
            if is_test and self.hparams.testdata.lower() == "aircapdata":
                import ipdb; ipdb.set_trace()
            
            return summ_pred_image, summ_in_image


    def test_dataloader(self):
        # OPTIONAL
        if self.hparams.testdata.lower() == "aircapdata":
            aircap_dset = aircapData.aircapData_crop()
            return DataLoader(aircap_dset, batch_size=self.hparams.val_batch_size,
                                num_workers=self.hparams.num_workers,
                                pin_memory=self.hparams.pin_memory,
                                drop_last=False)
        else:
            train_dset, val_dset = aerialpeople.get_aerialpeople_seqsplit(self.hparams.datapath)
            train_dloader = DataLoader(train_dset, batch_size=self.hparams.val_batch_size,
                                        num_workers=self.hparams.num_workers,
                                        pin_memory=self.hparams.pin_memory,
                                        shuffle=False,
                                        drop_last=True)
            test_dloader = DataLoader(val_dset, batch_size=self.hparams.val_batch_size,
                                        num_workers=self.hparams.num_workers,
                                        pin_memory=self.hparams.pin_memory,
                                        shuffle=False,
                                        drop_last=True)

            return [test_dloader, train_dloader]


    def test_step(self, batch, batch_idx, dset_idx=0):
        # OPTIONAL
        output, losses, loss = self.fwd_pass_and_loss(batch,is_val=True,is_test=True)
        # if self.hparams.testdata.lower() == "aircapdata":
        #     train_summ_pred_image, train_summ_in_image = self.summaries(batch, output, losses, is_test=True)
        # cv2.imwrite(train_summ_pred_image.permute(1,2,0).data.numpy())
        
        return {"test_loss" : loss,
                "output" : output}

    def test_epoch_end(self, outputs):
        global smplx
        test_err_smpltrans = np.array([(x["output"]["pred_smpltrans"] - 
            x["output"]["gt_smpltrans"]).cpu().numpy() for x in outputs[0]]).reshape(-1,3)
        mean_test_err_smpltrans = np.mean(np.sqrt(np.sum(test_err_smpltrans**2,1)))

        train_err_smpltrans = np.array([(x["output"]["pred_smpltrans"] - 
            x["output"]["gt_smpltrans"]).cpu().numpy() for x in outputs[1]]).reshape(-1,3)
        mean_train_err_smpltrans = np.mean(np.sqrt(np.sum(train_err_smpltrans**2,1)))


        smplpose_rotmat = torch.stack([x["output"]["smplpose_rotmat"].to(device) for x in outputs[0]])
        smplorient_rel = torch.stack([x["output"]["smplorient_rel"].to(device) for x in outputs[0]])

        pred_angles_test = torch.cat([x["output"]["pred_angles"].to(device) for x in outputs[0]])
        pred_rotmat_test = tgm.angle_axis_to_rotation_matrix(pred_angles_test.view(-1,3)).view(smplpose_rotmat.shape[0],smplpose_rotmat.shape[1],22,4,4)

        # pred_angles0_train = torch.cat([x["output"]["pred_angles0"].to(device) for x in outputs[1]])
        # pred_angles1_train = torch.cat([x["output"]["pred_angles1"].to(device) for x in outputs[1]])
        # pred_rotmat0_train = tgm.angle_axis_to_rotation_matrix(pred_angles0_train.view(-1,3)).view(pred_angles0_train.shape[0],22,4,4)
        # pred_rotmat1_train = tgm.angle_axis_to_rotation_matrix(pred_angles1_train.view(-1,3)).view(pred_angles1_train.shape[0],22,4,4)
    

        joints3d = []
        from tqdm import tqdm
        for i in tqdm(range(smplpose_rotmat.shape[0])):
            out_gt = smplx.forward(body_pose=smplpose_rotmat[i],
                                    global_orient= smplorient_rel[i],pose2rot=False)
            out_pred = smplx.forward(body_pose=pred_rotmat_test[i,:,1:22,:3,:3],
                                    global_orient= pred_rotmat_test[i,:,0:1,:3,:3],pose2rot=False)
            
            joints3d.append(np.stack([out_gt.joints.detach().cpu().numpy(),
                        out_pred.joints.detach().cpu().numpy()]).transpose(1,0,2,3))

        j3d = np.stack(joints3d).reshape(-1,4,127,3)[:,:,:22]
        print("test_mpjpe0: {}".format(np.mean(np.sqrt(np.sum((j3d[:,0] - j3d[:,2])**2,2))[:,:22])))
        print("test_mpjpe1: {}".format(np.mean(np.sqrt(np.sum((j3d[:,1] - j3d[:,3])**2,2))[:,:22])))
        print("test_mpe0: {}".format(mean_test_err_smpltrans))
        print("test_mpe1: {}".format(mean_train_err_smpltrans))

        # print("train_mpe00: {}".format(mean_test_err_smpltrans1))
        # print("train_mpjpe0: {}".format(mean_test_err_smplangles1))
        # print("train_mpe1: {}".format(mean_train_err_smpltrans1))
        # print("train_mpjpe1: {}".format(mean_train_err_smplangles1))

        # import ipdb; ipdb.set_trace()
        return {"outputs":outputs}


    @staticmethod
    def add_model_specific_args(parent_parser):
        """
        Specify the hyperparams for this LightningModule
        """
        # MODEL specific
        parser = ArgumentParser(parents=[parent_parser], add_help=False)

        req = parser.add_argument_group('Required')
        req.add_argument('--name', required=True, help='Name of the experiment')
        req.add_argument('--version', required=True, help='Version of the experiment')

        gen = parser.add_argument_group('General')
        gen.add_argument('--time_to_run', type=int, default=np.inf, help='Total time to run in seconds. Used for training in environments with timing constraints')
        gen.add_argument('--num_workers', type=int, default=8, help='Number of processes used for data loading')
        pin = gen.add_mutually_exclusive_group()
        pin.add_argument('--pin_memory', dest='pin_memory', action='store_true')
        pin.add_argument('--no_pin_memory', dest='pin_memory', action='store_false')
        gen.set_defaults(pin_memory=True)

        train = parser.add_argument_group('Training Options')
        train.add_argument('--datapath', type=str, default=None, help='Path to the dataset')
        train.add_argument('--model', type=str, default=None, required=True, help='model type')
        train.add_argument('--copenet_home', default='/home/jimmy/projects/AirPose/copenet', type=str, required=True, help='copenet repo home')
        train.add_argument('--log_dir', default='/home/jimmy/projects/AirPose/logs', help='Directory to store logs')
        train.add_argument('--testdata', type=str, default="aerialpeople", help='test dataset')
        train.add_argument("--lr", type=float, default=5e-5, help="Learning rate")
        train.add_argument('--batch_size', type=int, default=30, help='Batch size')
        train.add_argument('--val_batch_size', type=int, default=30, help='Validation data batch size')
        train.add_argument('--summary_steps', type=int, default=100, help='Summary saving frequency')
        train.add_argument('--val_summary_steps', type=float, default=10, help='validation summary frequency')
        train.add_argument('--checkpoint_steps', type=int, default=10000, help='Checkpoint saving frequency')
        train.add_argument('--img_res', type=int, default=224, help='Rescale bounding boxes to size [img_res, img_res] before feeding them in the network') 
        train.add_argument('--shape_loss_weight', default=1, type=float, help='Weight of per-vertex loss') 
        train.add_argument('--keypoint2d_loss_weight', default=0.001, type=float, help='Weight of 2D keypoint loss')
        train.add_argument('--keypoint3d_loss_weight', default=1, type=float, help='Weight of 3D keypoint loss')
        train.add_argument('--limbs3d_loss_weight', default=3., type=float, help='Weight of limbs 3D keypoint loss')
        train.add_argument('--limbstheta_loss_weight', default=3., type=float, help='Weight of limbs rotation angle loss')
        train.add_argument('--cam_noise_sigma', default=[0,0], type=list, help='noise sigma to add to gt cams (trans and rot)')
        train.add_argument('--smpltrans_noise_sigma', default=None, help='noise sigma to add to smpltrans')
        train.add_argument('--theta_noise_sigma', default=0.2, type=float, help='noise sigma to add to smpl thetas')
        train.add_argument('--trans_loss_weight', default=1, type=float, help='Weight of SMPL translation loss') 
        train.add_argument('--rootrot_loss_weight', default=1, type=float, help='Weight of SMPL root rotation loss') 
        train.add_argument('--pose_loss_weight', default=1, type=float, help='Weight of SMPL pose loss') 
        train.add_argument('--beta_loss_weight', default=1, type=float, help='Weight of SMPL betas loss')
        train.add_argument('--cams_loss_weight', default=1, type=float, help='Weight of cams params loss')
        train.add_argument('--reg_iters', default=3, type=int, help='number of regressor iterations')
        train.add_argument('--gt_train_weight', default=1., help='Weight for GT keypoints during training')
        train.add_argument('--train_reg_only_epochs', default=-1, help='number of epochs the regressor only part to be initially trained')  

        shuffle_train = train.add_mutually_exclusive_group()
        shuffle_train.add_argument('--shuffle_train', dest='shuffle_train', action='store_true', help='Shuffle training data')
        shuffle_train.add_argument('--no_shuffle_train', dest='shuffle_train', action='store_false', help='Don\'t shuffle training data')
        shuffle_train.set_defaults(shuffle_train=True)

        return parser
    
    def get_depth_aware(self, input_batch, pred_smpltrans, pred_rotmat, pred_output_cam):
        with torch.no_grad():
            transf_mat = torch.cat([pred_rotmat[:,:1].squeeze(1),
                                pred_smpltrans.unsqueeze(2)],dim=2)

            _,pred_joints_cam,_,_ = transform_smpl(transf_mat,
                                                pred_output_cam.vertices.squeeze(1),
                                                pred_output_cam.joints.squeeze(1))
            
            gt_joints_2d_cam = input_batch['smpl_joints_2d0'].squeeze(1)
            intr = input_batch['intr0']

            root_z_cam, root_z_cam_std = depth_aware(pred_joints_cam, gt_joints_2d_cam, torch.tensor(CONSTANTS.FOCAL_LENGTH, device=device).float(), intr[:,:2,2])
            
            # loss_depth_aware = self.mseloss(pred_smpltrans, root_xyz_cam).mean()
        return root_z_cam, root_z_cam_std

def slide_window_deambiguity(sorted_z_candicates, window_size):
    min_range = torch.full(sorted_z_candicates.shape[:-1], float('inf'), device=device)  # 窗口内极差的最小值
    min_avg = torch.full(sorted_z_candicates.shape[:-1], -1.0, device=device)
    min_std = torch.full(sorted_z_candicates.shape[:-1], -1.0, device=device)
    len = sorted_z_candicates.size(-1)

    for i in range(len - window_size + 1):
        cur_range = sorted_z_candicates[..., i + window_size - 1] - sorted_z_candicates[..., i]
        
        if min(cur_range) == float('inf'): 
            break
        
        update_flag = cur_range < min_range
        min_avg[update_flag] = torch.mean(sorted_z_candicates[..., i:i + window_size], dim=-1)[update_flag]
        min_std[update_flag] =  torch.std(sorted_z_candicates[..., i:i + window_size], dim=-1)[update_flag]
        min_range[update_flag] = cur_range[update_flag]

    # min_range_std = torch.std

    return min_avg, min_std, min_range


def depth_aware(j3d, j2d, cam_f, cam_center):
    batch_size = 30
    joints_num = 22

    f = torch.mean(cam_f[:2], dim=-1, keepdim=True)
    
    pose2d = j2d[:,:joints_num,:] - cam_center.reshape(batch_size, 1, 2).repeat(1, joints_num, 1)
    pose2d = torch.cat((pose2d, torch.zeros((batch_size, joints_num, 1), device=device, requires_grad=False)), dim=-1)
    org = torch.zeros((batch_size, joints_num, 3), device=device, requires_grad=False)
    org[:,:,2] = torch.mean(cam_f[:2].reshape(1, 1, 2).repeat(batch_size, joints_num, 1), dim=-1)
    OD = org - pose2d
    OD_norm = torch.norm(OD, dim=-1)
    CD = pose2d[:,0,:].reshape(batch_size, 1, 3).repeat(1, joints_num, 1) - pose2d
    CD_norm = torch.norm(CD, dim=-1)
    cos_alpha = torch.sum(OD * CD, dim=-1) / (OD_norm * CD_norm)
    
    pose3d = j3d[:,:joints_num,:] - j3d[:,:1,:]
    z_rel = pose3d[:,:,2]
    cos_alpha[z_rel > 0] *= -1

    AB = torch.norm(pose3d, dim=-1)
    
    BE = z_rel * OD_norm / f
    AB_proj_AE = BE ** 2 * (cos_alpha ** 2 - 1) + AB **2

    # 虽然计算为负值，但应该接近于0
    # 该情形对应关节AB接近与光轴平行
    minus_mask = AB_proj_AE < 0
    if minus_mask.sum().item() > 0:
        # print('warning: AB_proj_AE < 0; minus_mask.sum().item() = {}'.format(minus_mask.sum().item()))
        # print('负值定位为：{}'.format(torch.nonzero(minus_mask)))
        # print('最大负值（绝对值最大）：{}'.format(torch.min(AB_proj_AE[minus_mask])))
        AB_proj_AE[minus_mask] = 0
    
    AE = BE * cos_alpha + torch.sqrt(AB_proj_AE)
    AE_ = BE * cos_alpha - torch.sqrt(AB_proj_AE)
    z = AE / CD_norm * f; z = z[:, 1:]
    z_ = AE_ / CD_norm * f; z_ = z_[:, 1:]

    z_candicates = torch.full(AB.shape[:-1]+((AB.shape[-1]-1)*2,), float('inf'), device=device)
    obtuse_flag = (cos_alpha <= 0)[:, 1:]
    longer_flag = (AB >= BE)[:, 1:]
    unique_flag = obtuse_flag | longer_flag
    one_flag = torch.nonzero(unique_flag)
    two_flag = torch.nonzero(~unique_flag)

    one_flag_expand = one_flag.clone()
    two_flag_expand = two_flag.clone()
    one_flag_expand[:, -1] *= 2
    two_flag_expand[:, -1] *= 2

    z_candicates[one_flag_expand[:,0], one_flag_expand[:,1]] = z[one_flag[:,0], one_flag[:,1]]
    z_candicates[two_flag_expand[:,0], two_flag_expand[:,1]] = z[two_flag[:,0], two_flag[:,1]]
    two_flag_expand[:, -1] += 1
    z_candicates[two_flag_expand[:,0], two_flag_expand[:,1]] = z_[two_flag[:,0], two_flag[:,1]]                

    z_candicates = torch.sort(z_candicates, dim=-1)[0]
    z_abs_mean, z_abs_std, _ = slide_window_deambiguity(z_candicates, joints_num - 1)

    # z_abs_maxdiff = z_abs[:, -1] - z_abs[:, 0]

    # z_abs = torch.mean(z_abs, dim=-1, keepdim=True)
    # z_abs *= 0.05  # distance scaling
    # xy_abs = pose2d[:,0,:2] / f * z_abs.repeat(1, 2)
    # xyz_abs = torch.cat((xy_abs, z_abs), dim=-1)
    return z_abs_mean, z_abs_std