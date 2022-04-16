''''''
import time

'''
@Author: Astrophil (luo19902567292@163.com)
@Date: 2022-04-16
@LastEditTime: 2022-04-16
@LastEditors: Astrophil
@Description: This file contains evaluation tool for KITTI odometry
'''

import copy
import matplotlib as mpl
from matplotlib import pyplot as plt
import numpy as np
import os
from glob import glob

from matplotlib.backends.backend_agg import FigureCanvasAgg
import cv2
import pandas as pd
import xlrd
import heapq


def scale_lse_solver(X, Y):
    """最小平方误差解算器
    计算最佳比例因子，使s(X)-Y最小

    Args:
        X (array, [KxN]): current data
        Y (array, [KxN]): reference data
    
    Returns:
        scale (float): scaling factor
    """
    scale = np.sum(X * Y)/np.sum(X ** 2)
    return scale


def umeyama_alignment(x, y, with_scale=False):
    """计算Sim(m)矩阵的最小二乘解参数，该矩阵最小化一组注册点之间的距离。
    :param x: mxn matrix of points, m = dimension, n = nr. of data points
    :param y: mxn matrix of points, m = dimension, n = nr. of data points
    :param with_scale: set to True to align also the scale (default: 1.0 scale)
    :return: r, t, c - rotation matrix, translation vector and scale factor
    """
    if x.shape != y.shape:
        assert False, "x.shape not equal to y.shape"

    # m = dimension, n = nr. of data points
    m, n = x.shape

    # means, eq. 34 and 35
    mean_x = x.mean(axis=1)
    mean_y = y.mean(axis=1)

    # variance, eq. 36
    # "transpose" for column subtraction
    sigma_x = 1.0 / n * (np.linalg.norm(x - mean_x[:, np.newaxis])**2)

    # covariance matrix, eq. 38
    outer_sum = np.zeros((m, m))
    for i in range(n):
        outer_sum += np.outer((y[:, i] - mean_y), (x[:, i] - mean_x))
    cov_xy = np.multiply(1.0 / n, outer_sum)

    # SVD (text betw. eq. 38 and 39)
    u, d, v = np.linalg.svd(cov_xy)

    # S matrix, eq. 43
    s = np.eye(m)
    if np.linalg.det(u) * np.linalg.det(v) < 0.0:
        # Ensure a RHS coordinate system (Kabsch algorithm).
        s[m - 1, m - 1] = -1

    # rotation, eq. 40
    r = u.dot(s).dot(v)

    # scale & translation, eq. 42 and 41
    c = 1 / sigma_x * np.trace(np.diag(d).dot(s)) if with_scale else 1.0
    t = mean_y - np.multiply(c, r.dot(mean_x))

    return r, t, c


class KittiEvalOdom():
    """评估里程计
    """
    def __init__(self):
        self.lengths = [100, 200, 300, 400, 500, 600, 700, 800]
        self.num_lengths = len(self.lengths)
        self.result = []

    def load_poses_from_txt(self, file_name):
        """从txt加载姿势（KITTI格式）
        文件中的每一行都应该遵循以下结构之一
            (1) idx姿态（12个数字的3x4矩阵）
            (2) 姿态（12个数字的3x4矩阵）

        Args:
            file_name (str): txt file path
        
        Returns:
            poses (dict): {idx: [4x4] array}
        """
        f = open(file_name, 'r')
        s = f.readlines()
        f.close()
        poses = {}
        for cnt, line in enumerate(s):
            P = np.eye(4)
            line_split = [float(i) for i in line.split(" ") if i!=""]
            withIdx = len(line_split) == 13
            for row in range(3):
                for col in range(4):
                    P[row, col] = line_split[row*4 + col + withIdx]
            if withIdx:
                frame_idx = line_split[0]
            else:
                frame_idx = cnt
            poses[frame_idx] = P
        return poses

    def trajectory_distances(self, poses):
        """计算每个姿势的距离w.r.t frame-0

        Args:
            poses (dict): {idx: 4x4 array}
        
        Returns:
            dist (list): distance of each pose w.r.t frame-0
        """
        dist = [0]
        sort_frame_idx = sorted(poses.keys())
        for i in range(len(sort_frame_idx)-1):
            cur_frame_idx = sort_frame_idx[i]
            next_frame_idx = sort_frame_idx[i+1]
            P1 = poses[cur_frame_idx]
            P2 = poses[next_frame_idx]
            dx = P1[0, 3] - P2[0, 3]
            dy = P1[1, 3] - P2[1, 3]
            dz = P1[2, 3] - P2[2, 3]
            dist.append(dist[i]+np.sqrt(dx**2+dy**2+dz**2))
        return dist

    def rotation_error(self, pose_error):
        """计算旋转误差
        
        Args:
            pose_error (array, [4x4]): relative pose error
        
        Returns:
            rot_error (float): rotation error
        """
        a = pose_error[0, 0]
        b = pose_error[1, 1]
        c = pose_error[2, 2]
        d = 0.5*(a+b+c-1.0)
        rot_error = np.arccos(max(min(d, 1.0), -1.0))
        return rot_error

    def translation_error(self, pose_error):
        """计算平移错误
        
        Args:
            pose_error (array, [4x4]): relative pose error
        
        Returns:
            trans_error (float): translation error
        """
        dx = pose_error[0, 3]
        dy = pose_error[1, 3]
        dz = pose_error[2, 3]
        trans_error = np.sqrt(dx**2+dy**2+dz**2)
        return trans_error

    def last_frame_from_segment_length(self, dist, first_frame, length):
        """找到距离第一个_帧有所需距离的帧（索引）
        
        Args:
            dist (list): distance of each pose w.r.t frame-0
            first_frame (int): start-frame index
            length (float): required distance
        
        Returns:
            i (int): end-frame index. if not found return -1
        """
        for i in range(first_frame, len(dist), 1):
            if dist[i] > (dist[first_frame] + length):
                return i
        return -1

    def calc_sequence_errors(self, poses_gt, poses_result):
        """计算序列误差
        
        Args:
            poses_gt (dict): {idx: 4x4 array}, ground truth poses
            poses_result (dict): {idx: 4x4 array}, predicted poses
        
        Returns:
            a list of list containing [first_frame, rotation error, translation error, length, speed]
                - **first_frame** (int): frist frame index
                - **rotation error** (float): rotation error per length
                - **translation error** (float): translation error per length
                - **length** (float): evaluation trajectory length
                - **speed** (float): car speed (#FIXME: 10FPS is assumed)
        """
        err = []
        dist = self.trajectory_distances(poses_gt)
        self.step_size = 10

        for first_frame in range(0, len(poses_gt), self.step_size):
            for i in range(self.num_lengths):
                len_ = self.lengths[i]
                last_frame = self.last_frame_from_segment_length(
                                        dist, first_frame, len_
                                        )

                # Continue if sequence not long enough
                if last_frame == -1 or \
                        not(last_frame in poses_result.keys()) or \
                        not(first_frame in poses_result.keys()):
                    continue

                # compute rotational and translational errors
                pose_delta_gt = np.dot(
                                    np.linalg.inv(poses_gt[first_frame]),
                                    poses_gt[last_frame]
                                    )
                pose_delta_result = np.dot(
                                        np.linalg.inv(poses_result[first_frame]),
                                        poses_result[last_frame]
                                        )
                pose_error = np.dot(
                                np.linalg.inv(pose_delta_result),
                                pose_delta_gt
                                )

                r_err = self.rotation_error(pose_error)
                t_err = self.translation_error(pose_error)

                # compute speed
                num_frames = last_frame - first_frame + 1.0
                speed = len_/(0.1*num_frames)

                err.append([first_frame, r_err/len_, t_err/len_, len_, speed])
        return err
        
    def save_sequence_errors(self, err, file_name):
        """保存序列错误
        
        Args:
            err (list): a list of list containing error information
            file_name (str): txt file for writing errors
        """
        fp = open(file_name, 'w')
        for i in err:
            line_to_write = " ".join([str(j) for j in i])
            fp.writelines(line_to_write+"\n")
        fp.close()

    def save_RPE_errors(self, err, file_name):
        """保存序列错误
        
        Args:
            err (dict): error information
            file_name (str): txt file for writing errors
        """
        fp = open(file_name, 'w')
        trans_errors = err['trans']
        rot_errors = err['rot']
        for i in range(len(trans_errors)):
            line_to_write = " ".join([str(j) for j in [trans_errors[i], rot_errors[i]*180/np.pi]])
            fp.writelines(line_to_write+"\n")
        fp.close()

    def compute_overall_err(self, seq_err):
        """计算平均平移和旋转误差
        
        Args:
            seq_err (list), a list of list containing [r_err, t_err]
                
                - **r_err** (float): rotation error
                - **t_err** (float): translation error
        
        Returns:
            a tuple containing 
                - **ave_t_err** (float): average translation error
                - **ave_r_err** (float): average rotation error
        """
        t_err = 0
        r_err = 0

        seq_len = len(seq_err)

        if seq_len > 0:
            for item in seq_err:
                r_err += item[1]
                t_err += item[2]
            ave_t_err = t_err / seq_len
            ave_r_err = r_err / seq_len
            return ave_t_err, ave_r_err
        else:
            return 0, 0

    def plot_trajectory(self, poses_gt, poses_result, seq):
        """绘制GT和预测的轨迹
        
        Args:
            poses_gt (dict): {idx: 4x4 array}; ground truth poses
            poses_result (dict): {idx: 4x4 array}; predicted poses
            seq (int): sequence index.
        """
        plot_keys = ["Ground Truth", "Ours"]
        fontsize_ = 20

        poses_dict = {}
        poses_dict["Ground Truth"] = poses_gt
        poses_dict["Ours"] = poses_result

        color_list = {"Ground Truth": 'k',
                      "Ours": 'lime'}
        linestyle = {"Ground Truth": "--",
                     "Ours": "-"}

        fig = plt.figure()
        ax = plt.gca()
        ax.set_aspect('equal')

        for key in plot_keys:
            pos_xyz = []
            frame_idx_list = sorted(poses_dict["Ours"].keys())
            for frame_idx in frame_idx_list:
                # pose = np.linalg.inv(poses_dict[key][frame_idx_list[0]]) @ poses_dict[key][frame_idx]
                pose = poses_dict[key][frame_idx]
                pos_xyz.append([pose[0, 3], pose[1, 3], pose[2, 3]])
            pos_xyz = np.asarray(pos_xyz)
            plt.plot(pos_xyz[:, 0],  pos_xyz[:, 2], label=key, c=color_list[key], linestyle=linestyle[key])

            # Draw rect
            if key == 'Ground Truth':
                rect = mpl.patches.Rectangle((pos_xyz[0, 0]-5, pos_xyz[0, 2]-5), 10,10, linewidth=2, edgecolor='k', facecolor='none')
                ax.add_patch(rect)

        plt.legend(loc="upper right", prop={'size': fontsize_})
        plt.xticks(fontsize=fontsize_)
        plt.yticks(fontsize=fontsize_)
        plt.xlabel('x (m)', fontsize=fontsize_)
        plt.ylabel('z (m)', fontsize=fontsize_)
        plt.grid(linestyle="--")
        fig.set_size_inches(10, 10)
        png_title = "sequence_{}".format(seq)
        fig_pdf = self.plot_path_dir + "/" + png_title + ".pdf"
        canvas = FigureCanvasAgg(plt.gcf())
        canvas.draw()
        self.img_sequence_path = np.array(canvas.renderer.buffer_rgba())
        if self.if_save == True:
            plt.savefig(fig_pdf, bbox_inches='tight', pad_inches=0)
            img_save_path = self.plot_path_dir + "/" + png_title + ".jpg"
            cv2.imwrite(img_save_path, self.img_sequence_path)
        # cv2.imshow('img', self.img)
        plt.close(fig)

    def plot_error(self, avg_segment_errs, seq):
        """按长度绘制错误
        
        Args:
            avg_segment_errs (dict): {100:[avg_t_err, avg_r_err],...}
            seq (int): sequence index.
        """
        # Translation error
        plot_y = []
        plot_x = []
        for len_ in self.lengths:
            plot_x.append(len_)
            if len(avg_segment_errs[len_]) > 0:
                plot_y.append(avg_segment_errs[len_][0] * 100)
            else:
                plot_y.append(0)
        fontsize_ = 10
        fig = plt.figure()
        plt.plot(plot_x, plot_y, "bs-", label="Translation Error")
        plt.ylabel('Translation Error (%)', fontsize=fontsize_)
        plt.xlabel('Path Length (m)', fontsize=fontsize_)
        plt.legend(loc="upper right", prop={'size': fontsize_})
        fig.set_size_inches(5, 5)
        fig_pdf = self.plot_error_dir + "/trans_err_{}.pdf".format(seq)
        canvas = FigureCanvasAgg(plt.gcf())
        canvas.draw()
        self.img_trans_err = np.array(canvas.renderer.buffer_rgba())
        if self.if_save == True:
            plt.savefig(fig_pdf, bbox_inches='tight', pad_inches=0)
            img_save_path = self.plot_error_dir + "/trans_err_{}.jpg".format(seq)
            cv2.imwrite(img_save_path, self.img_trans_err)
        plt.close(fig)

        # Rotation error
        plot_y = []
        plot_x = []
        for len_ in self.lengths:
            plot_x.append(len_)
            if len(avg_segment_errs[len_]) > 0:
                plot_y.append(avg_segment_errs[len_][1] / np.pi * 180 * 100)
            else:
                plot_y.append(0)
        fontsize_ = 10
        fig = plt.figure()
        plt.plot(plot_x, plot_y, "bs-", label="Rotation Error")
        plt.ylabel('Rotation Error (deg/100m)', fontsize=fontsize_)
        plt.xlabel('Path Length (m)', fontsize=fontsize_)
        plt.legend(loc="upper right", prop={'size': fontsize_})
        fig.set_size_inches(5, 5)
        fig_pdf = self.plot_error_dir + "/rot_err_{}.pdf".format(seq)
        canvas = FigureCanvasAgg(plt.gcf())
        canvas.draw()
        self.img_rot_err = np.array(canvas.renderer.buffer_rgba())
        if self.if_save == True:
            plt.savefig(fig_pdf, bbox_inches='tight', pad_inches=0)
            img_save_path = self.plot_error_dir + "/rot_err_{}.jpg".format(seq)
            cv2.imwrite(img_save_path, self.img_rot_err)
        plt.close(fig)

    def compute_segment_error(self, seq_errs):
        """此函数计算不同段的平均误差。
        
        Args:
            seq_errs (list): a list of list containing [first_frame, rotation error, translation error, length, speed]
            
                - **first_frame** (int): frist frame index
                - **rotation error** (float): rotation error per length
                - **translation error** (float): translation error per length
                - **length** (float): evaluation trajectory length
                - **speed** (float): car speed (#FIXME: 10FPS is assumed)
        
        Returns:
            avg_segment_errs (dict): {100:[avg_t_err, avg_r_err],...}    
        """

        segment_errs = {}
        avg_segment_errs = {}
        for len_ in self.lengths:
            segment_errs[len_] = []

        # Get errors
        for err in seq_errs:
            len_ = err[3]
            t_err = err[2]
            r_err = err[1]
            segment_errs[len_].append([t_err, r_err])

        # Compute average
        for len_ in self.lengths:
            if segment_errs[len_] != []:
                avg_t_err = np.mean(np.asarray(segment_errs[len_])[:, 0])
                avg_r_err = np.mean(np.asarray(segment_errs[len_])[:, 1])
                avg_segment_errs[len_] = [avg_t_err, avg_r_err]
            else:
                avg_segment_errs[len_] = []
        return avg_segment_errs

    def compute_ATE(self, gt, pred):
        """计算ATE的RMSE
        
        Args:
            gt (dict): ground-truth poses as [4x4] array
            pred (dict): predicted poses as [4x4] array
        """
        errors = []
        idx_0 = list(pred.keys())[0]
        gt_0 = gt[idx_0]
        pred_0 = pred[idx_0]

        for i in pred:
            # cur_gt = np.linalg.inv(gt_0) @ gt[i]
            cur_gt = gt[i]
            gt_xyz = cur_gt[:3, 3] 

            # cur_pred = np.linalg.inv(pred_0) @ pred[i]
            cur_pred = pred[i]
            pred_xyz = cur_pred[:3, 3]

            align_err = gt_xyz - pred_xyz

            errors.append(np.sqrt(np.sum(align_err ** 2)))
        ate = np.sqrt(np.mean(np.asarray(errors) ** 2)) 
        return ate
    
    def compute_RPE(self, gt, pred):
        """计算 RPE
        
        Args:
            gt (dict): ground-truth poses as [4x4] array
            pred (dict): predicted poses as [4x4] array
        
        Returns:
            trans_errors (list): list of rpe translation error
            rot_errors (list): list of RPE rotation error
        """
        rpe_errors = {'trans': [], 'rot': []}
        pred_keys = list(pred.keys())
        for cnt in range(len(pred_keys)-1):
            gt1 = gt[pred_keys[cnt]]
            gt2 = gt[pred_keys[cnt+1]]
            gt_rel = np.linalg.inv(gt1) @ gt2

            pred1 = pred[pred_keys[cnt]]
            pred2 = pred[pred_keys[cnt+1]]
            pred_rel = np.linalg.inv(pred1) @ pred2
            rel_err = np.linalg.inv(gt_rel) @ pred_rel
            
            rpe_errors['trans'].append(self.translation_error(rel_err))
            rpe_errors['rot'].append(self.rotation_error(rel_err))
        return rpe_errors

    def scale_optimization(self, gt, pred):
        """ 优化比例因子
        
        Args:
            gt (dict): ground-truth poses as [4x4] array
            pred (dict): predicted poses as [4x4] array
        
        Returns:
            new_pred (dict): predicted poses after optimization as [4x4] array
        """
        pred_updated = copy.deepcopy(pred)
        xyz_pred = []
        xyz_ref = []
        for i in pred:
            pose_pred = pred[i]
            pose_ref = gt[i]
            xyz_pred.append(pose_pred[:3, 3])
            xyz_ref.append(pose_ref[:3, 3])
        xyz_pred = np.asarray(xyz_pred)
        xyz_ref = np.asarray(xyz_ref)
        scale = scale_lse_solver(xyz_pred, xyz_ref)
        for i in pred_updated:
            pred_updated[i][:3, 3] *= scale
        return pred_updated
    
    def compute_trajectory_length(self, gt):
        """计算轨迹长度
        
        Args:
            gt (dict): ground-truth poses as [4x4] array
        
        Returns:
            length (float): trajectory length
        """
        length = 0.

        for i in range(len(gt)-1):
            cur_gt = gt[i]
            next_gt = gt[i+1]

            length += np.linalg.norm(cur_gt[:3, 3] - next_gt[:3, 3])
        return length
    
    def write_result(self, f, seq, errs):
        """将结果写入txt文件
        
        Args:
            f (IOWrapper)
            seq (int): sequence number
            errs (list): [ave_t_err, ave_r_err, ate, rpe_trans, rpe_rot]
        """
        ave_t_err, ave_r_err, ate, rpe_trans, rpe_rot = errs
        lines = []
        lines.append("Sequence: \t {} \n".format(seq) )
        lines.append("Trans. err. (%): \t {:.3f} \n".format(ave_t_err*100))
        lines.append("Rot. err. (deg/100m): \t {:.3f} \n".format(ave_r_err/np.pi*180*100))
        lines.append("ATE (m): \t {:.3f} \n".format(ate))
        lines.append("RPE (m): \t {:.3f} \n".format(rpe_trans))
        lines.append("RPE (deg): \t {:.3f} \n\n".format(rpe_rot * 180 /np.pi))
        for line in lines:
            f.writelines(line)

    def Rank(self, result_data):
        """生成排名

        :param result_data:
        :return:
        """
        rt = []
        for index in range(len(result_data)):
            ours = result_data[index][-1]
            rank = 1
            for i in result_data[index][:-1]:
                if i < ours:
                    rank += 1
            rt.append(rank)
        return rt

    def Excel_comparison(self):
        """生成Excel实验对比列表

        """
        dict_ = {}
        raw_data = pd.read_excel(
            self.excel_raw_data_path,
            # skiprows=[1, ],
            # names=None
        )

        # 获取实验数据
        column = ['00', '01', '02', '03', '04', '05', '06', '07', '08', '09', '10', 'Avg. Err.']
        data = raw_data.loc[:, column].values
        len_data = len(data)

        # 获取方法名称
        methods = raw_data.loc[:, 'Method']
        methods = methods.dropna(how= 'all')
        methods = methods.values.tolist()
        # 添加ours
        methods.append('Tested data')
        self.methods = methods

        # 将原始数据分为几个list
        TErr = []  # Translational error (%)
        RErr = []  # Rotational error (deg/100m)
        ATE = []  # ATE (m)
        RPE_m = []  # RPE (m)
        RPE_deg = []  # RPE (deg)
        seq_index = column.index(self.eval_seq)
        for i in range(int(len_data / 5)):
            TErr.append(data[i * 5][seq_index])
            RErr.append(data[i * 5 + 1][seq_index])
            ATE.append(data[i * 5 + 2][seq_index])
            RPE_m.append(data[i * 5 + 3][seq_index])
            RPE_deg.append(data[i * 5 + 4][seq_index])

        # 添加被测数据进入list
        TErr.append(self.result[1])
        RErr.append(self.result[2])
        ATE.append(self.result[3])
        RPE_m.append(self.result[4])
        RPE_deg.append(self.result[5])

        # 现有实验数据长度
        len_data += 1

        # 制表
        result_data = [TErr, RErr, ATE, RPE_m, RPE_deg]
        # 返回各指标排位信息。并转为numpy
        rank = np.array(self.Rank(result_data))
        self.rank = rank
        rank = rank[:, np.newaxis]  # rank升一个维度，5x1

        result_data = np.array(result_data) # [5， 方法数量（包括自己的）]
        result_data = np.c_[result_data, rank]  # 沿着矩阵列拼接矩阵
        self.result_data = result_data

        result_data = pd.DataFrame(result_data)

        # 列表标题
        result_data_columns = []
        print(methods)
        for method in methods:
            result_data_columns.append(method)
        result_data_columns.append('Ours Rank')
        result_data.columns = result_data_columns
        # 列表指标
        result_data.index = ['terr', 'rerr', 'ATE', 'RPE(m)', 'RPE(°)']

        writer = pd.ExcelWriter(self.excel_save_path)
        result_data.to_excel(writer, float_format='%.3f')
        writer.save()

    def eval(self, gt_dir, result_dir,
                alignment=None,
                seq=None,
                if_save=False):
        """评估所需/可用序列
        
        Args:
            gt_dir (str): ground truth poses txt files directory
            result_dir (str): pose predictions txt files directory
            alignment (str): if not None, optimize poses by

                - scale: optimize scale factor for trajectory alignment and evaluation
                - scale_7dof: optimize 7dof for alignment and use scale for trajectory evaluation
                - 7dof: optimize 7dof for alignment and evaluation
                - 6dof: optimize 6dof for alignment and evaluation
                
            seqs (list): 
            
                - None: Evalute all available seqs in result_dir
                - list: list of sequence indexs to be evaluated
            if_save (False):

                - True: 保存结果文件
                - False: 默认是不保存结果文件
        """
        # seq_list = ["{:02}".format(i) for i in range(0, 11)]

        # Initialization
        self.gt_dir = gt_dir
        ave_t_errs = []
        ave_r_errs = []
        seq_ate = []
        seq_rpe_trans = []
        seq_rpe_rot = []
        avg_seq_lens = []

        # Create result directory
        error_dir = result_dir + seq + "/errors"
        self.plot_path_dir = result_dir + seq + "/plot_path"
        self.plot_error_dir = result_dir + seq + "/plot_error"
        # result_txt = os.path.join(result_dir, seq, "/result.txt")
        result_txt = "{}{}/result.txt".format(result_dir, seq)

        # excel
        self.excel_raw_data_path = "dataset/kitti_odom/other_experiments.xlsx"
        self.excel_save_path = "{}{}/result.xlsx".format(result_dir, seq)


        self.if_save = if_save

        if not os.path.exists(error_dir):
            os.makedirs(error_dir)
        if not os.path.exists(self.plot_path_dir):
            os.makedirs(self.plot_path_dir)
        if not os.path.exists(self.plot_error_dir):
            os.makedirs(self.plot_error_dir)

        f = open(result_txt, 'w')

        # Create evaluation list
        if seq is None:
            available_seqs = sorted(glob(os.path.join(result_dir, "*.txt")))
            self.eval_seq = []
            for seq in available_seqs:
                if not("result" in seq.split("/")[-1]):
                    self.eval_seq.append(seq.split("/")[-1].split(".")[0])
        else:
            self.eval_seq = seq


        # evaluation
        i = self.eval_seq
        # Read pose txt
        file_name = '{}.txt'.format(i)

        poses_result = self.load_poses_from_txt(result_dir+"/"+file_name)
        poses_gt = self.load_poses_from_txt(self.gt_dir + "/" + file_name)
        self.result_file_name = result_dir+file_name

        # Pose alignment to first frame
        idx_0 = sorted(list(poses_result.keys()))[0]
        pred_0 = poses_result[idx_0]
        gt_0 = poses_gt[idx_0]
        for cnt in poses_result:
            poses_result[cnt] = np.linalg.inv(pred_0) @ poses_result[cnt]
            poses_gt[cnt] = np.linalg.inv(gt_0) @ poses_gt[cnt]

        if alignment == "scale":
            poses_result = self.scale_optimization(poses_gt, poses_result)
        elif alignment == "scale_7dof" or alignment == "7dof" or alignment == "6dof":
            # get XYZ
            xyz_gt = []
            xyz_result = []
            for cnt in poses_result:
                xyz_gt.append([poses_gt[cnt][0, 3], poses_gt[cnt][1, 3], poses_gt[cnt][2, 3]])
                xyz_result.append([poses_result[cnt][0, 3], poses_result[cnt][1, 3], poses_result[cnt][2, 3]])
            xyz_gt = np.asarray(xyz_gt).transpose(1, 0)
            xyz_result = np.asarray(xyz_result).transpose(1, 0)

            r, t, scale = umeyama_alignment(xyz_result, xyz_gt, alignment!="6dof")

            align_transformation = np.eye(4)
            align_transformation[:3:, :3] = r
            align_transformation[:3, 3] = t

            for cnt in poses_result:
                poses_result[cnt][:3, 3] *= scale
                if alignment=="7dof" or alignment=="6dof":
                    poses_result[cnt] = align_transformation @ poses_result[cnt]

        # compute sequence length
        seq_len = self.compute_trajectory_length(poses_gt)
        avg_seq_lens.append(seq_len)

        # compute sequence errors
        seq_err = self.calc_sequence_errors(poses_gt, poses_result)
        self.save_sequence_errors(seq_err, error_dir + "/" + file_name)

        # Compute segment errors
        avg_segment_errs = self.compute_segment_error(seq_err)

        # compute overall error
        ave_t_err, ave_r_err = self.compute_overall_err(seq_err)
        print("Sequence: " + str(i))
        print("Seq. Lenght (m): ", seq_len)
        self.result.append(seq_len)
        print("Translational error (%): ", ave_t_err*100)
        self.result.append(ave_t_err*100)
        print("Rotational error (deg/100m): ", ave_r_err/np.pi*180*100)
        self.result.append(ave_r_err/np.pi*180*100)
        print("-"*20)
        ave_t_errs.append(ave_t_err)
        ave_r_errs.append(ave_r_err)

        # Compute ATE
        ate = self.compute_ATE(poses_gt, poses_result)
        seq_ate.append(ate)
        print("ATE (m): ", ate)
        self.result.append(ate)

        # Compute RPE
        rpe_errors = self.compute_RPE(poses_gt, poses_result)
        rpe_trans = np.mean(np.asarray(rpe_errors['trans']))
        rpe_rot = np.mean(np.asarray(rpe_errors['rot']))
        self.save_RPE_errors(rpe_errors, error_dir + "/" + "RPE_"+file_name)
        seq_rpe_trans.append(rpe_trans)
        seq_rpe_rot.append(rpe_rot)
        print("RPE (m): ", rpe_trans)
        print("RPE (deg): ", rpe_rot * 180 /np.pi)
        self.result.append(rpe_trans)
        self.result.append(rpe_rot * 180 /np.pi)

        # Plotting
        self.plot_trajectory(poses_gt, poses_result, i)
        self.plot_error(avg_segment_errs, i)

        # Save result summary
        self.write_result(f, i, [ave_t_err, ave_r_err, ate, rpe_trans, rpe_rot])
            
        f.close()    

        print("-------------------- For Copying ------------------------------")
        for i in range(len(ave_t_errs)):
            print("{0:.2f}".format(ave_t_errs[i]*100))
            print("{0:.2f}".format(ave_r_errs[i]/np.pi*180*100))
            print("{0:.2f}".format(seq_ate[i]))
            print("{0:.3f}".format(seq_rpe_trans[i]))
            print("{0:.3f}".format(seq_rpe_rot[i] * 180 / np.pi))

        # excel对比
        self.Excel_comparison()

        # 图片显示
        print(np.shape(self.img_sequence_path))
        print(np.shape(self.img_trans_err))
        print(np.shape(self.img_rot_err))

        err_img = np.vstack((self.img_trans_err, self.img_rot_err))  # 竖直堆叠
        imgs = np.hstack((self.img_sequence_path, err_img))  # 水平堆叠
        # imgs = np.hstack([self.img_sequence_path, self.img_trans_err])
        cv2.imshow('imgs', imgs)

        return self.result, self.rank, self.methods, self.result_data
