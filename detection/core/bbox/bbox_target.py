import numpy as np
import tensorflow as tf

from detection.core.bbox import geometry, transforms
from detection.utils.misc import *

class ProposalTarget(object):
    def __init__(self, target_means, target_stds, num_rcnn_deltas=512):
        '''Compute regression and classification targets for proposals.
        
        Attributes
        ---
            target_means: [4]. Bounding box refinement mean for RCNN.
                Example: (0., 0., 0., 0.)
            target_stds: [4]. Bounding box refinement standard deviation for RCNN.
                Example: (0.1, 0.1, 0.2, 0.2)
            num_rcnn_deltas: int. Maximal number of RoIs per image to feed to bbox heads.
        '''
        self.target_means = tf.constant(target_means)
        self.target_stds = tf.constant(target_stds)
        self.num_rcnn_deltas = 512
        self.roi_positive_fraction = 0.25
        self.pos_iou_thr = 0.5
        self.neg_iou_thr = 0.5
            
    def build_targets(self, proposals_list, gt_boxes, gt_class_ids, img_metas):
        '''Generates detection targets for images. Subsamples proposals and
        generates target class IDs, bounding box deltas for each.
        
        Args
        ---
            proposals_list: list of [num_proposals, (y1, x1, y2, x2)] in normalized coordinates.
            gt_boxes: [batch_size, num_gt_boxes, (y1, x1, y2, x2)] in image coordinates.
            gt_class_ids: [batch_size, num_gt_boxes] Integer class IDs.
            img_metas: [batch_size, 11]
            
        Returns
        ---
            rois_list: list of [num_rois, (y1, x1, y2, x2)] in normalized coordinates
            rcnn_target_matchs_list: list of [num_rois]. Integer class IDs.
            rcnn_target_deltas_list: list of [num_positive_rois, (dy, dx, log(dh), log(dw))].
            
        Note that self.num_rcnn_deltas >= num_rois > num_positive_rois. And different 
           images in one batch may have different num_rois and num_positive_rois.
        '''
        
        img_shapes = calc_img_shapes(img_metas)
        
        rois_list = []
        rcnn_target_matchs_list = []
        rcnn_target_deltas_list = []
        
        for i in range(img_metas.shape[0]):
            rois, target_matchs, target_deltas = self._build_single_target(
                proposals_list[i], gt_boxes[i], gt_class_ids[i], img_shapes[i])
            rois_list.append(rois)
            rcnn_target_matchs_list.append(target_matchs)
            rcnn_target_deltas_list.append(target_deltas)
        
        return rois_list, rcnn_target_matchs_list, rcnn_target_deltas_list
    
    def _build_single_target(self, proposals, gt_boxes, gt_class_ids, img_shape):
        '''
        Args
        ---
            proposals: [num_proposals, (y1, x1, y2, x2)] in normalized coordinates.
            gt_boxes: [num_gt_boxes, (y1, x1, y2, x2)]
            gt_class_ids: [num_gt_boxes]
            img_shape: np.ndarray. [2]. (img_height, img_width)
            
        Returns
        ---
            rois: [num_rois, (y1, x1, y2, x2)]
            target_matchs: [num_positive_rois]
            target_deltas: [num_positive_rois, (dy, dx, log(dh), log(dw))]
        '''
        H, W = img_shape
        
        
        gt_boxes, non_zeros = trim_zeros(gt_boxes)
        gt_class_ids = tf.boolean_mask(gt_class_ids, non_zeros)
        
        gt_boxes = gt_boxes / tf.constant([H, W, H, W], dtype=tf.float32)
        
        overlaps = geometry.compute_overlaps(proposals, gt_boxes)
        anchor_iou_argmax = tf.argmax(overlaps, axis=1)
        roi_iou_max = tf.reduce_max(overlaps, axis=1)

        positive_roi_bool = (roi_iou_max >= self.pos_iou_thr)
        positive_indices = tf.where(positive_roi_bool)[:, 0]
        
        negative_indices = tf.where(roi_iou_max < self.neg_iou_thr)[:, 0]
        
        # Subsample ROIs. Aim for 33% positive
        # Positive ROIs
        positive_count = int(self.num_rcnn_deltas * self.roi_positive_fraction)
        positive_indices = tf.random_shuffle(positive_indices)[:positive_count]
        positive_count = tf.shape(positive_indices)[0]
        
        # Negative ROIs. Add enough to maintain positive:negative ratio.
        r = 1.0 / self.roi_positive_fraction
        negative_count = tf.cast(r * tf.cast(positive_count, tf.float32), tf.int32) - positive_count
        negative_indices = tf.random_shuffle(negative_indices)[:negative_count]
        
        # Gather selected ROIs
        positive_rois = tf.gather(proposals, positive_indices)
        negative_rois = tf.gather(proposals, negative_indices)
        
        # Assign positive ROIs to GT boxes.
        positive_overlaps = tf.gather(overlaps, positive_indices)
        roi_gt_box_assignment = tf.argmax(positive_overlaps, axis=1)
        roi_gt_boxes = tf.gather(gt_boxes, roi_gt_box_assignment)
        target_matchs = tf.gather(gt_class_ids, roi_gt_box_assignment)
        
        
        target_deltas = transforms.bbox2delta(positive_rois, roi_gt_boxes, self.target_means, self.target_stds)
        
        rois = tf.concat([positive_rois, negative_rois], axis=0)
        
        N = tf.shape(negative_rois)[0]
        target_matchs = tf.pad(target_matchs, [(0, N)])
        
        target_matchs = tf.stop_gradient(target_matchs)
        target_deltas = tf.stop_gradient(target_deltas)
        
        return rois, target_matchs, target_deltas