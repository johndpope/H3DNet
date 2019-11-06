# coding: utf-8
# Copyright (c) Facebook, Inc. and its affiliates.
# 
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

""" Dataset for object bounding box regression.
An axis aligned bounding box is parameterized by (cx,cy,cz) and (dx,dy,dz)
where (cx,cy,cz) is the center point of the box, dx is the x-axis length of the box.
"""
import os
import sys
import numpy as np
from torch.utils.data import Dataset
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
sys.path.append(ROOT_DIR)
sys.path.append(os.path.join(ROOT_DIR, 'utils'))
import pc_util
from model_util_scannet import rotate_aligned_boxes

from model_util_scannet import ScannetDatasetConfig

from scipy.optimize import linear_sum_assignment
from scipy.optimize import leastsq

from scipy.cluster.vq import vq, kmeans, whiten

DC = ScannetDatasetConfig()
MAX_NUM_OBJ = 64
MEAN_COLOR_RGB = np.array([109.8, 97.2, 83.8])

def f_min(X,p):
    plane_xyz = p[0:3]
    distance = (plane_xyz*X.T).sum(axis=1) + p[3]
    return distance / np.linalg.norm(plane_xyz)

def residuals(params, signal, X):
    return f_min(X, params)

def params2bbox(center, xsize, ysize, zsize, angle):
    ''' from bbox_center, angle and size to bbox
    @Args:
        center: (3)
        x/y/zsize: scalar
        angle: -pi ~ pi
    @Returns:
        bbox: 8 x 3, order:
         [[xmin, ymin, zmin], [xmin, ymin, zmax], [xmin, ymax, zmin], [xmin, ymax, zmax],
          [xmax, ymin, zmin], [xmax, ymin, zmax], [xmax, ymax, zmin], [xmax, ymax, zmax]]
    '''
    vx = np.array([np.cos(angle), np.sin(angle), 0])
    vy = np.array([-np.sin(angle), np.cos(angle), 0])
    vx = vx * np.abs(xsize) / 2
    vy = vy * np.abs(ysize) / 2
    vz = np.array([0, 0, np.abs(zsize) / 2])
    bbox = np.array([\
        center - vx - vy - vz, center - vx - vy + vz,
        center - vx + vy - vz, center - vx + vy + vz,
        center + vx - vy - vz, center + vx - vy + vz,
        center + vx + vy - vz, center + vx + vy + vz])
    return bbox

def pdist2(x1, x2):
    """ Computes the squared Euclidean distance between all pairs """
    C = -2*np.matmul(x1,x2.T)
    nx = np.sum(np.square(x1),1,keepdims=True)
    ny = np.sum(np.square(x2),1,keepdims=True)
    costMatrix = (C + ny.T) + nx
    return costMatrix

def find_idx(targetbb, selected_centers, selected_centers_support, selected_centers_bsupport):
    center_matrix = np.stack(selected_centers)
    assert(center_matrix.shape[0] == targetbb.shape[0])
    #costMatrix = np.zeros((selected_centers.shape[0], selected_centers.shape[0]))
    costMatrix = pdist2(center_matrix, targetbb)
    row_ind, col_ind = linear_sum_assignment(costMatrix)
    idx2bb = {i:row_ind[i] for i in range(center_matrix.shape[0])}
    support_idx = []
    bsupport_idx = []
    for center in selected_centers_support:
        check = 0
        for idx in range(len(selected_centers)):
            if np.array_equal(selected_centers[idx], center):
                check = 1
                break
        if check == 0:
            print("error with data")
        if idx not in support_idx:
            support_idx.append(int(idx2bb[idx]))
    for center in selected_centers_bsupport:
        check = 0
        for idx in range(len(selected_centers)):
            if np.array_equal(selected_centers[idx], center):
                check = 1
                break
        if check == 0:
            print("error with data")
        if idx not in bsupport_idx:
            bsupport_idx.append(int(idx2bb[idx]))
    return support_idx, bsupport_idx

class ScannetDetectionDataset(Dataset):
       
    def __init__(self, split_set='train', num_points=20000, center_dev=2.0, corner_dev=1.0,
                 use_color=False, use_height=False, augment=False, use_angle=False, vsize=0.06, use_tsdf=0, use_18cls=1):

        # self.data_path = os.path.join(BASE_DIR, 'scannet_train_detection_data')
        self.data_path = os.path.join('/scratch/cluster/yanght/Dataset/', 'scannet_train_detection_data')
        self.data_path_vox = os.path.join('/scratch/cluster/bosun/data/scannet/', 'scannet_train_detection_data')
        all_scan_names = list(set([os.path.basename(x)[0:12] \
            for x in os.listdir(self.data_path) if x.startswith('scene')]))
        if split_set=='all':            
            self.scan_names = all_scan_names
        elif split_set in ['train', 'val', 'test']:
            split_filenames = os.path.join(ROOT_DIR, 'scannet/meta_data',
                'scannetv2_{}.txt'.format(split_set))
            with open(split_filenames, 'r') as f:
                self.scan_names = f.read().splitlines()   
            # remove unavailiable scans
            num_scans = len(self.scan_names)
            self.scan_names = [sname for sname in self.scan_names \
                if sname in all_scan_names]
            print('kept {} scans out of {}'.format(len(self.scan_names), num_scans))
            num_scans = len(self.scan_names)
        else:
            print('illegal split name')
            return
        
        self.num_points = num_points
        self.use_color = use_color        
        self.use_height = use_height
        self.use_angle = use_angle
        self.augment = augment

        ### Vox parameters
        self.vsize = vsize
        self.center_dev = center_dev
        self.corner_dev = corner_dev
        self.use_tsdf = use_tsdf
        self.use_18cls = use_18cls
        
    def __len__(self):
        return len(self.scan_names)

    def __getitem__(self, idx):
        """
        Returns a dict with following keys:
            point_clouds: (N,3+C)
            center_label: (MAX_NUM_OBJ,3) for GT box center XYZ
            sem_cls_label: (MAX_NUM_OBJ,) semantic class index
            angle_class_label: (MAX_NUM_OBJ,) with int values in 0,...,NUM_HEADING_BIN-1
            angle_residual_label: (MAX_NUM_OBJ,)
            size_classe_label: (MAX_NUM_OBJ,) with int values in 0,...,NUM_SIZE_CLUSTER
            size_residual_label: (MAX_NUM_OBJ,3)
            box_label_mask: (MAX_NUM_OBJ) as 0/1 with 1 indicating a unique box
            point_votes: (N,3) with votes XYZ
            point_votes_mask: (N,) with 0/1 with 1 indicating the point is in one of the object's OBB.
            scan_idx: int scan index in scan_names list
            pcl_color: unused
        """
        
        scan_name = self.scan_names[idx]        
        mesh_vertices = np.load(os.path.join(self.data_path, scan_name)+'_vert.npy')
        plane_vertices = np.load(os.path.join(self.data_path, scan_name)+'_plane.npy')
        ### Without ori
        if self.use_angle:
            meta_vertices = np.load(os.path.join(self.data_path, scan_name)+'_all_angle_40cls.npy') ### Need to change the name here
        else:
            ### With ori
            meta_vertices = np.load(os.path.join(self.data_path, scan_name)+'_all_noangle_40cls.npy') ### Need to change the name here
        
        ### Load voxel data
        sem_vox=np.load(os.path.join(self.data_path_vox, scan_name+'_vox_0.06_sem.npy'))
        vox = np.array(sem_vox>0,np.float32)
        if self.use_angle:
            vox_center = np.load(os.path.join(self.data_path_vox, scan_name+'_vox_0.06_center_angle_18.npy'))
            vox_corner = np.load(os.path.join(self.data_path_vox, scan_name+'_vox_0.06_corner_angle_18.npy'))
        else:
            vox_center = np.load(os.path.join(self.data_path_vox, scan_name+'_vox_0.06_center_noangle_18.npy'))
            vox_corner = np.load(os.path.join(self.data_path_vox, scan_name+'_vox_0.06_corner_noangle_18.npy'))

        instance_labels = meta_vertices[:,-2]
        semantic_labels = meta_vertices[:,-1]
        #instance_labels = np.load(os.path.join(self.data_path, scan_name)+'_ins_label.npy')
        #semantic_labels = np.load(os.path.join(self.data_path, scan_name)+'_sem_label.npy')
        #support_labels = np.load(os.path.join(self.data_path, scan_name)+'_support_label.npy')
        #support_instance_labels = np.load(os.path.join(self.data_path, scan_name)+'_support_instance_label.npy')
        #instance_bboxes = np.load(os.path.join(self.data_path, scan_name)+'_bbox.npy')
        
        if not self.use_color:
            point_cloud = mesh_vertices[:,0:3] # do not use color for now
            pcl_color = mesh_vertices[:,3:6]
        else:
            point_cloud = mesh_vertices[:,0:6] 
            point_cloud[:,3:] = (point_cloud[:,3:]-MEAN_COLOR_RGB)/256.0
            pcl_color = (point_cloud[:,3:]-MEAN_COLOR_RGB)/256.0
        
        if self.use_height:
            floor_height = np.percentile(point_cloud[:,2],0.99)
            height = point_cloud[:,2] - floor_height
            point_cloud = np.concatenate([point_cloud, np.expand_dims(height, 1)],1) 
        # ------------------------------- LABELS ------------------------------        
        target_bboxes = np.zeros((MAX_NUM_OBJ, 6))
        target_bboxes_mask = np.zeros((MAX_NUM_OBJ))    
        angle_classes = np.zeros((MAX_NUM_OBJ,))
        angle_residuals = np.zeros((MAX_NUM_OBJ,))
        size_classes = np.zeros((MAX_NUM_OBJ,))
        size_residuals = np.zeros((MAX_NUM_OBJ, 3))

        before_sample = np.unique(instance_labels)
        while True:
            orig_point_cloud = np.copy(point_cloud)
            temp_point_cloud, choices = pc_util.random_sampling(orig_point_cloud,
                                                           self.num_points, return_choices=True)
            after_sample = np.unique(instance_labels[choices])
            if np.array_equal(before_sample, after_sample):
                point_cloud = temp_point_cloud
                break
        instance_labels = instance_labels[choices]
        semantic_labels = semantic_labels[choices]
        plane_vertices = plane_vertices[choices]
        meta_vertices = meta_vertices[choices]
        
        pcl_color = pcl_color[choices]

        #target_bboxes[0:instance_bboxes.shape[0],:] = instance_bboxes[:,0:6]
        
        # ------------------------------- DATA AUGMENTATION ------------------------------        
        # if False:#self.augment:## Do not use augment for now
        point_yz = -1
        point_xz = -1
        point_rot = np.eye(3).astype(np.float32)
        if self.augment:
            if np.random.random() > 0.5:
                # Flipping along the YZ plane
                point_yz = 1
                point_cloud[:,0] = -1 * point_cloud[:,0]
                plane_vertices[:,0] = -1 * plane_vertices[:,0]
                # target_bboxes[:,0] = -1 * target_bboxes[:,0]                
                meta_vertices[:, 0] = -1 * meta_vertices[:, 0]                
                meta_vertices[:, 6] = -1 * meta_vertices[:, 6]
                
            if np.random.random() > 0.5:
                # Flipping along the XZ plane
                point_xz = 1
                point_cloud[:,1] = -1 * point_cloud[:,1]
                plane_vertices[:,1] = -1 * plane_vertices[:,1]
                # target_bboxes[:,1] = -1 * target_bboxes[:,1]
                meta_vertices[:, 1] = -1 * meta_vertices[:, 1]
                meta_vertices[:, 6] = -1 * meta_vertices[:, 6]
            
            # Rotation along up-axis/Z-axis
            rot_angle = (np.random.random()*np.pi/18) - np.pi/36 # -5 ~ +5 degree
            rot_mat = pc_util.rotz(rot_angle).astype(np.float32)
            point_rot = rot_mat
            point_cloud[:,0:3] = np.dot(point_cloud[:,0:3], np.transpose(rot_mat))
            plane_vertices[:,0:3] = np.transpose(np.dot(rot_mat, np.transpose(plane_vertices[:,0:3])))
            meta_vertices[:, :6] = rotate_aligned_boxes(meta_vertices[:, :6], rot_mat)
            meta_vertices[:, 6] += rot_angle
        # load voxel data 
        vox = pc_util.point_cloud_to_voxel_scene(point_cloud[:,0:3])
        bbx_for_vox = np.unique(meta_vertices, axis=0)
        bbx_for_vox_processed = pc_util.process_bbx(bbx_for_vox)
        # vox_center = pc_util.center_to_volume_gaussion(bbx_for_vox_processed, dev=self.center_dev)
        vox_center = pc_util.point_to_volume_gaussion(bbx_for_vox_processed[:, :3], dev=self.center_dev)
        corner_vox = pc_util.get_corner(bbx_for_vox_processed) # without angle 
        # corner_vox = pc_util.get_oriented_corners(bbx_for_vox) # with angle
        vox_corner = pc_util.point_to_volume_gaussion(corner_vox, dev=self.corner_dev)
        
        # ------------------------------- Plane and point ------------------------------
        # compute votes *AFTER* augmentation
        # generate votes
        # Note: since there's no map between bbox instance labels and
        # pc instance_labels (it had been filtered 
        # in the data preparation step) we'll compute the instance bbox
        # from the points sharing the same instance label. 
        point_votes = np.zeros([self.num_points, 3])
        point_votes_corner = np.zeros([self.num_points, 3])
        point_votes_mask = np.zeros(self.num_points)
        point_sem_label = np.zeros(self.num_points)
        
        ### Plane Patches
        plane_label = np.zeros([self.num_points, 3+4])
        plane_label_mask = np.zeros(self.num_points)
        plane_votes_front = np.zeros([self.num_points, 4])
        plane_votes_back = np.zeros([self.num_points, 4])
        plane_votes_left = np.zeros([self.num_points, 4])
        plane_votes_right = np.zeros([self.num_points, 4])
        plane_votes_upper = np.zeros([self.num_points, 4])
        plane_votes_lower = np.zeros([self.num_points, 4])

        '''
        plane_votes_rot_front = np.zeros([self.num_points, 3])
        plane_votes_rot_back = np.zeros([self.num_points, 3])
        plane_votes_rot_left = np.zeros([self.num_points, 3])
        plane_votes_rot_right = np.zeros([self.num_points, 3])
        plane_votes_rot_upper = np.zeros([self.num_points, 3])
        plane_votes_rot_lower = np.zeros([self.num_points, 3])

        plane_votes_off_front = np.zeros([self.num_points, 1])
        plane_votes_off_back = np.zeros([self.num_points, 1])
        plane_votes_off_left = np.zeros([self.num_points, 1])
        plane_votes_off_right = np.zeros([self.num_points, 1])
        plane_votes_off_upper = np.zeros([self.num_points, 1])
        plane_votes_off_lower = np.zeros([self.num_points, 1])
        '''
        
        #assert(num_instance == len(np.unique(instance_labels)) - 1)
        """
        if 0 in np.unique(instance_labels):
            if ((len(np.unique(instance_labels)) - 1 - 1)*2 == support_instance_labels.shape[1]) == False:
                import pdb;pdb.set_trace()
        else:
            assert((len(np.unique(instance_labels)) - 1)*2 == support_instance_labels.shape[1])
        """
        selected_instances = []
        selected_centers = []
        selected_centers_support = []
        selected_centers_bsupport = []
        obj_meta = []
        
        for i_instance in np.unique(instance_labels):            
            # find all points belong to that instance
            ind = np.where(instance_labels == i_instance)[0]
            # find the semantic label            
            if semantic_labels[ind[0]] in DC.nyu40ids:
                x = point_cloud[ind,:3]
                ### Meta information here
                meta = meta_vertices[ind[0]]
                obj_meta.append(meta)
                
                ### Get the centroid here
                center = meta[:3]

                ### Corners
                corners = params2bbox(center, meta[3], meta[4], meta[5], meta[6])
                
                point_votes[ind, :] = center - x
                point_votes_mask[ind] = 1.0
                point_sem_label[ind] = DC.nyu40id2class_sem[meta[-1]]
                
                xtemp = np.stack([x]*len(corners))
                dist = np.sum(np.square(xtemp - np.expand_dims(corners, 1)), axis=2)
                sel_corner = np.argmin(dist, 0)
                for i in range(len(ind)):
                    point_votes_corner[ind[i], :] = corners[sel_corner[i]] - x[i,:]
                selected_instances.append(i_instance)
                selected_centers.append(center)

                ### check for planes here
                '''
                @Returns:
                bbox: 8 x 3, order:
                [[xmin, ymin, zmin], [xmin, ymin, zmax], [xmin, ymax, zmin], [xmin, ymax, zmax],
                 [xmax, ymin, zmin], [xmax, ymin, zmax], [xmax, ymax, zmin], [xmax, ymax, zmax]]
                '''
                plane_indicator = plane_vertices[ind,4]
                planes = np.unique(plane_indicator)
                plane_ind = []
                for p in planes:
                    if p > 0:
                        temp_ind = np.where(plane_indicator == p)[0]
                        if len(temp_ind) > 10:
                            plane_ind.append(ind[temp_ind])
                            ### Normalize the vector here
                            ### May need to change later
                            #plane_vertices[ind[temp_ind],:4] = plane_vertices[ind[temp_ind],:4] / np.linalg.norm(plane_vertices[ind[temp_ind][0],:3])
                if len(plane_ind) > 0:
                    plane_ind = np.concatenate(plane_ind, 0)
                    plane_label_mask[plane_ind] = 1.0
                    #plane_vertices[plane_ind,:4] = plane_vertices[plane_ind,:4]# / np.linalg.norm(plane_vertices[plane_ind[0],:], -1)
                    plane_lower = leastsq(residuals, [0,0,1,0], args=(None, np.array([corners[0], corners[2], corners[4], corners[6]]).T))[0]
                    #plane_upper = leastsq(residuals, plane_lower, args=(None, np.array([corners[1], corners[3], corners[5], corners[7]]).T))[0]
                    para_points = np.array([corners[1], corners[3], corners[5], corners[7]])
                    newd = np.sum(para_points * plane_lower[:3], 1)
                    plane_upper = np.concatenate([plane_lower[:3], np.array([-np.mean(newd)])], 0)
                    
                    plane_left = leastsq(residuals, [1,0,0,0], args=(None, np.array([corners[0], corners[1], corners[2], corners[3]]).T))[0]
                    para_points = np.array([corners[4], corners[5], corners[6], corners[7]])
                    newd = np.sum(para_points * plane_left[:3], 1)
                    plane_right = np.concatenate([plane_left[:3], np.array([-np.mean(newd)])], 0)
                    #plane_right = leastsq(residuals, plane_left, args=(None, np.array([corners[4], corners[5], corners[6], corners[7]]).T))[0]
                    plane_front = leastsq(residuals, [0,1,0,0], args=(None, np.array([corners[0], corners[1], corners[4], corners[5]]).T))[0]
                    para_points = np.array([corners[2], corners[3], corners[6], corners[7]])
                    newd = np.sum(para_points * plane_front[:3], 1)
                    plane_back = np.concatenate([plane_front[:3], np.array([-np.mean(newd)])], 0)
                    #plane_back = leastsq(residuals, plane_front, args=(None, np.array([corners[2], corners[3], corners[6], corners[7]]).T))[0]

                    plane_votes_upper[plane_ind,:] = plane_upper# / plane_upper[-1]
                    plane_votes_lower[plane_ind,:] = plane_lower# / plane_lower[-1]
                    plane_votes_front[plane_ind,:] = plane_front# / plane_front[-1]
                    plane_votes_back[plane_ind,:] = plane_back# / plane_back[-1]
                    plane_votes_left[plane_ind,:] = plane_left# / plane_left[-1]
                    plane_votes_right[plane_ind,:] = plane_right# / plane_right[-1]
                    #import pdb;pdb.set_trace()
                    '''
                    xyz = np.array([corners[2], corners[3], corners[6], corners[7]])
                    pc_util.write_ply_label(point_cloud, np.zeros(point_cloud.shape[0]), 'just_plane_1.ply', 1)
                    pc_util.write_ply_label(xyz, np.zeros(xyz.shape[0]), 'just_plane_2.ply', 1)
                    new_xyz = np.array([corners[0], corners[1], corners[4], corners[5]])
                    pc_util.write_ply_label(new_xyz, np.zeros(new_xyz.shape[0]), 'just_plane_3.ply', 1)
                    import pdb;pdb.set_trace()
                    xy = xyz[:,:2]
                    z = -(np.sum(plane_back[:2]*xy, 1) + plane_back[3]) / plane_back[2]
                    new_xyz = np.concatenate([xy,np.expand_dims(z, -1)], 1)
                    
                    pc_util.write_ply_label(point_cloud, np.zeros(point_cloud.shape[0]), 'just_plane_1.ply', 1)
                    pc_util.write_ply_label(new_xyz, np.zeros(new_xyz.shape[0]), 'just_plane_2.ply', 1)

                    choice = np.random.choice(point_cloud.shape[0], 1000, replace=False)
                    ### get z
                    xy = point_cloud[choice,:2]
                    z = -(np.sum(plane_back[:2]*xy, 1) + plane_back[3]) / plane_back[2]
                    new_xyz = np.concatenate([xy,np.expand_dims(z, -1)], 1)
                    pc_util.write_ply_label(new_xyz, np.zeros(new_xyz.shape[0]), 'just_plane_3.ply', 1)
                    import pdb;pdb.set_trace()
                    '''
                    '''
                    import pdb;pdb.set_trace()
                    viz_plane(np.concatenate([point_cloud[:,:3], plane_vertices[:,:4]], 1), plane_label_mask,name=str(i_example)+'plane')
                    cmap = viz_plane(np.concatenate([point_cloud[:,:3], plane_votes_upper], 1), plane_label_mask,name=str(i_example)+'plane_oneside')
                    import pdb;pdb.set_trace()
                    '''
        num_instance = len(obj_meta)
        obj_meta = np.array(obj_meta)
        obj_meta = obj_meta.reshape(-1, 9)

        target_bboxes_mask[0:num_instance] = 1
        target_bboxes[0:num_instance,:6] = obj_meta[:,0:6]
        class_ind = [np.where(DC.nyu40ids == x)[0][0] for x in obj_meta[:,-1]]   
        # NOTE: set size class as semantic class. Consider use size2class.
        size_classes[0:num_instance] = class_ind
        size_residuals[0:num_instance, :] = \
                                            target_bboxes[0:num_instance, 3:6] - DC.mean_size_arr[class_ind,:]
        # angle_classes[0:num_instance] = class_ind
        # angle_residuals[0:num_instance] = obj_meta[:,6]
        for i in range(num_instance):
            angle_class, angle_residual = DC.angle2class2(obj_meta[i, 6])
            angle_classes[i] = angle_class
            angle_residuals[i] = angle_residual
            assert np.abs(DC.class2angle2(angle_class, angle_residual) - obj_meta[i, 6]) < 1e-6

        
        point_votes = np.tile(point_votes, (1, 3)) # make 3 votes identical
        point_sem_label = np.tile(np.expand_dims(point_sem_label, -1), (1, 3)) # make 3 votes identical
        point_votes_corner = np.tile(point_votes_corner, (1, 3)) # make 3 votes identical

        plane_votes_rot_front = np.tile(plane_votes_front[:,:3], (1, 3)) # make 3 votes identical
        plane_votes_off_front = np.tile(np.expand_dims(plane_votes_front[:,3], -1), (1, 3)) # make 3 votes identical

        plane_votes_rot_back = np.tile(plane_votes_back[:,:3], (1, 3)) # make 3 votes identical
        plane_votes_off_back = np.tile(np.expand_dims(plane_votes_back[:,3], -1), (1, 3)) # make 3 votes identical

        plane_votes_rot_lower = np.tile(plane_votes_lower[:,:3], (1, 3)) # make 3 votes identical
        plane_votes_off_lower = np.tile(np.expand_dims(plane_votes_lower[:,3], -1), (1, 3)) # make 3 votes identical

        plane_votes_rot_upper = np.tile(plane_votes_upper[:,:3], (1, 3)) # make 3 votes identical
        plane_votes_off_upper = np.tile(np.expand_dims(plane_votes_upper[:,3], -1), (1, 3)) # make 3 votes identical

        plane_votes_rot_left = np.tile(plane_votes_left[:,:3], (1, 3)) # make 3 votes identical
        plane_votes_off_left = np.tile(np.expand_dims(plane_votes_left[:,3], -1), (1, 3)) # make 3 votes identical

        plane_votes_rot_right = np.tile(plane_votes_right[:,:3], (1, 3)) # make 3 votes identical
        plane_votes_off_right = np.tile(np.expand_dims(plane_votes_right[:,3], -1), (1, 3)) # make 3 votes identical

        ret_dict = {}
                
        ret_dict['point_clouds'] = point_cloud.astype(np.float32)
        ret_dict['center_label'] = target_bboxes.astype(np.float32)[:,0:3]
        ret_dict['heading_class_label'] = angle_classes.astype(np.int64)
        ret_dict['heading_residual_label'] = angle_residuals.astype(np.float32)
        ret_dict['size_class_label'] = size_classes.astype(np.int64)
        ret_dict['size_residual_label'] = size_residuals.astype(np.float32)
        
        target_bboxes_semcls = np.zeros((MAX_NUM_OBJ))                                
        target_bboxes_semcls[0:num_instance] = \
            [DC.nyu40id2class[x] for x in obj_meta[:,-1][0:obj_meta.shape[0]]]                
        ret_dict['sem_cls_label'] = target_bboxes_semcls.astype(np.int64)

        ret_dict['point_sem_cls_label'] = point_sem_label.astype(np.int64)
        ret_dict['box_label_mask'] = target_bboxes_mask.astype(np.float32)

        ret_dict['vote_label'] = point_votes.astype(np.float32)
        ret_dict['vote_label_corner'] = point_votes_corner.astype(np.float32)
        ret_dict['vote_label_mask'] = point_votes_mask.astype(np.int64)

        ret_dict['plane_label'] = np.concatenate([point_cloud, plane_vertices[:,:4]], 1).astype(np.float32)
        ret_dict['plane_label_mask'] = plane_label_mask.astype(np.float32)
        ret_dict['plane_votes_rot_front'] = plane_votes_rot_front.astype(np.float32)
        ret_dict['plane_votes_off_front'] = plane_votes_off_front.astype(np.float32)
        
        ret_dict['plane_votes_rot_back'] = plane_votes_rot_back.astype(np.float32)
        ret_dict['plane_votes_off_back'] = plane_votes_off_back.astype(np.float32)
        
        ret_dict['plane_votes_rot_left'] = plane_votes_rot_left.astype(np.float32)
        ret_dict['plane_votes_off_left'] = plane_votes_off_left.astype(np.float32)
        
        ret_dict['plane_votes_rot_right'] = plane_votes_rot_right.astype(np.float32)
        ret_dict['plane_votes_off_right'] = plane_votes_off_right.astype(np.float32)
        
        ret_dict['plane_votes_rot_lower'] = plane_votes_rot_lower.astype(np.float32)
        ret_dict['plane_votes_off_lower'] = plane_votes_off_lower.astype(np.float32)
        
        ret_dict['plane_votes_rot_upper'] = plane_votes_rot_upper.astype(np.float32)
        ret_dict['plane_votes_off_upper'] = plane_votes_off_upper.astype(np.float32)
        
        ret_dict['scan_idx'] = np.array(idx).astype(np.int64)
        ret_dict['pcl_color'] = pcl_color
        ret_dict['num_instance'] = num_instance
        ret_dict['scan_name'] = scan_name

        ret_dict['voxel'] =np.expand_dims(vox.astype(np.float32), 0)
#         ret_dict['sem_voxel'] =np.array(sem_vox, np.float32)
        ret_dict['vox_center'] = np.expand_dims(np.array(vox_center, np.float32), 0)
        ret_dict['vox_corner'] = np.expand_dims(np.array(vox_corner, np.float32), 0)

        ret_dict['aug_yz'] = point_yz
        ret_dict['aug_xz'] = point_xz
        ret_dict['aug_rot'] = point_rot
        
        return ret_dict
        
############# Visualizaion ########

def viz_votes(pc, point_votes, point_votes_mask, name=''):
    """ Visualize point votes and point votes mask labels
    pc: (N,3 or 6), point_votes: (N,9), point_votes_mask: (N,)
    """
    inds = (point_votes_mask==1)
    pc_obj = pc[inds,0:3]
    pc_obj_voted1 = pc_obj + point_votes[inds,0:3]    
    pc_util.write_ply(pc_obj, 'pc_obj{}.ply'.format(name))
    pc_util.write_ply(pc_obj_voted1, 'pc_obj_voted1{}.ply'.format(name))

def viz_plane(point_planes, point_planes_mask, name=''):
    """ Visualize point votes and point votes mask labels
    pc: (N,3 or 6), point_votes: (N,9), point_votes_mask: (N,)
    """
    inds = (point_planes_mask==1)
    pc_plane = point_planes[inds,:]
    cmap = pc_util.write_ply_color_multi(pc_plane[:,:3], pc_plane[:,4:], 'pc_obj_planes{}.ply'.format(name))
    return cmap

def viz_plane_perside(point_planes, point_planes_mask, name=''):
    """ Visualize point votes and point votes mask labels
    pc: (N,3 or 6), point_votes: (N,9), point_votes_mask: (N,)
    """
    inds = (point_planes_mask==1)
    pc_plane = point_planes[inds,:]

    #whitened = whiten(pc_plane[:,3:7])
    '''
    planes = {}

    count = 0
    for plane in pc_plane[:,3:7]:
        check = 1
        for k in planes:
            if np.array_equal(planes[k], plane):
                check *= 0
                break
        if check == 1:
            planes[count] = plane
            count += 1
    '''
    planes = kmeans(pc_plane[:,3:7], 40)[0] ### Get 40 planes
    #temp_planes = []
    #for k in planes:
    #    temp_planes.append(planes[k])
    #planes = np.stack(temp_planes)
    #import pdb;pdb.set_trace()
    
    final_scene = pc_plane[:,:3]
    final_labels = np.zeros(pc_plane.shape[0])
    cur_scene = pc_plane[:,:3]
    count = 0
    for j in range(len(planes)):
        cur_plane = planes[j,:]#np.stack([planes[j,:]]*cur_scene.shape[0])
        if np.sum(cur_plane) == 0:
            continue
        ### Sample 1000 points
        choice = np.random.choice(cur_scene.shape[0], 500, replace=False)
        ### get z
        xy = cur_scene[choice,:2]
        z = -(np.sum(planes[j,:2]*xy, 1) + planes[j,3]) / planes[j,2]
        new_xyz = np.concatenate([xy,np.expand_dims(z, -1)], 1)
        #pc_util.write_ply_label(np.concatenate([final_scene, new_xyz], 0), np.concatenate([final_labels, np.ones(500)*(count+1)], 0), 'just_plane_%d.ply' % (j), count+2)
        #import pdb;pdb.set_trace()
        final_scene = np.concatenate([final_scene, new_xyz], 0)
        final_labels = np.concatenate([final_labels, np.ones(500)*(count+1)], 0)
        count += 1
    #pc_util.write_ply_label(cur_scene, np.squeeze(labels), '%d_plane_visual.ply' % i, len(planes))
    #import pdb;pdb.set_trace()
    pc_util.write_ply_label(final_scene, final_labels, 'pc_obj_planes_oneside{}.ply'.format(name), count+1)
    #cmap = pc_util.write_ply_color_multi(pc_plane[:,:3], pc_plane[:,3:], 'pc_obj_planes{}.ply'.format(name))
    
def viz_obb(pc, label, mask, angle_classes, angle_residuals,
    size_classes, size_residuals, name=''):
    """ Visualize oriented bounding box ground truth
    pc: (N,3)
    label: (K,3)  K == MAX_NUM_OBJ
    mask: (K,)
    angle_classes: (K,)
    angle_residuals: (K,)
    size_classes: (K,)
    size_residuals: (K,3)
    """
    oriented_boxes = []
    K = label.shape[0]
    for i in range(K):
        if mask[i] == 0: continue
        obb = np.zeros(7)
        obb[0:3] = label[i,0:3]
        heading_angle = 0 # hard code to 0
        box_size = DC.mean_size_arr[size_classes[i], :] + size_residuals[i, :]
        obb[3:6] = box_size
        obb[6] = -1 * heading_angle
        print(obb)        
        oriented_boxes.append(obb)
    pc_util.write_oriented_bbox(oriented_boxes, 'gt_obbs{}.ply'.format(name))
    pc_util.write_ply(label[mask==1,:], 'gt_centroids{}.ply'.format(name))

    
if __name__=='__main__': 
    dset = ScannetDetectionDataset(use_height=True, num_points=40000, augment=True)
    for i_example in range(1513):
        example = dset.__getitem__(i_example)
        pc_util.write_ply(example['point_clouds'], 'pc_{}.ply'.format(i_example))
        pc_util.write_ply_label(example['point_clouds'][:,:3], example['point_sem_cls_label'], 'pc_sem_{}.ply'.format(str(i_example)),  19)
        viz_votes(example['point_clouds'], example['vote_label'],
                  example['vote_label_mask'],name=i_example)
        viz_votes(example['point_clouds'], example['vote_label_corner'],
                  example['vote_label_mask'],name=str(i_example)+'corner')
        viz_plane(example['plane_label'],
                  example['plane_label_mask'],name=str(i_example)+'plane')
        #viz_plane(np.concatenate([example['plane_label'][:,:3], example['plane_votes_rot_upper']], 1),example['plane_label_mask'],name=str(i_example)+'plane_oneside')
        viz_plane_perside(np.concatenate([example['plane_label'][:,:3], np.concatenate([example['plane_votes_rot_upper'][:,:3], np.expand_dims(example['plane_votes_off_upper'][:,0], -1)], 1)], 1),
                  example['plane_label_mask'],name=str(i_example)+'plane_oneside')
        
        #viz_votes(example['point_clouds'], example['vote_label_support_middle'],example['vote_label_mask_support'],name=str(i_example)+'support_middle')
        #viz_votes(example['point_clouds'], example['vote_label_bsupport_middle'],example['vote_label_mask_bsupport'],name=str(i_example)+'bsupport_middle')
        #viz_votes(example['point_clouds'], example['vote_label_support_offset'],example['vote_label_mask_support'],name=str(i_example)+'support_offset')
        #viz_votes(example['point_clouds'], example['vote_label_bsupport_offset'],example['vote_label_mask_bsupport'],name=str(i_example)+'bsupport_offset')
        import pdb;pdb.set_trace()

        """
        viz_obb(pc=example['point_clouds'], label=example['center_label'],
            mask=example['box_label_mask'],
            angle_classes=None, angle_residuals=None,
            size_classes=example['size_class_label'], size_residuals=example['size_residual_label'],
            name=i_example)
        viz_obb(pc=example['point_clouds'], label=example['center_label_support'],
            mask=example['box_label_mask_support'],
            angle_classes=None, angle_residuals=None,
            size_classes=example['size_class_label_support'], size_residuals=example['size_residual_label_support'],
            name=str(i_example)+'support')
        viz_obb(pc=example['point_clouds'], label=example['center_label_bsupport'],
            mask=example['box_label_mask_bsupport'],
            angle_classes=None, angle_residuals=None,
            size_classes=example['size_class_label_bsupport'], size_residuals=example['size_residual_label_bsupport'],
            name=str(i_example)+'bsupport')
        import pdb;pdb.set_trace()
        """
        
