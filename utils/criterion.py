
import numpy as np

import torch

import torch.nn as nn
from scipy.optimize import linear_sum_assignment
from torchvision.ops.boxes import box_area
import torch.nn.functional as F

import warnings
warnings.filterwarnings('ignore')

device = 'cuda' if torch.cuda.is_available() else 'cpu'



class HungarianMatcher(nn.Module):
    """This class computes an assignment between the targets and the predictions of the network
    """

    def __init__(self, cost_class: float = 2, cost_bbox: float = 5, cost_point: float = 5, **kwargs):
        """Creates the matcher
        """
        super().__init__()
        self.cost_class = cost_class
        self.cost_bbox = cost_bbox
        self.cost_point = cost_point
        assert cost_class != 0 or cost_bbox != 0 or cost_point != 0, "all costs cant be 0"

    @torch.no_grad()
    def forward(self, outputs, targets): 
        """ Performs the matching
        Returns:
            A list of size batch_size, containing tuples of (index_i, index_j) where:
                - index_i is the indices of the selected predictions (in order)
                - index_j is the indices of the corresponding selected targets (in order)
            For each batch element, it holds:
                len(index_i) = len(index_j) = min(num_queries, num_target_boxes)
        """

        
        bs, num_queries = outputs["pred_logits"].shape[:2]

        # flatten to compute the cost matrices in a batch
        out_prob = outputs["pred_logits"].sigmoid().flatten(0, 1)  
        out_point = outputs["pred_points"].flatten(0, 1) 

        # Also concat the target labels and boxes
        tgt_ids = torch.cat([v["labels"] for v in targets]) 
        tgt_point = torch.cat([v["points"] for v in targets]) 

        """ class cost """
        out_prob_exp = out_prob.unsqueeze(1).expand(-1, tgt_ids.shape[0], -1)
        # change nan value of out_prob_exp to 0
        out_prob_exp[torch.isnan(out_prob_exp)] = 0
        tgt_ids_exp = tgt_ids.unsqueeze(0).expand(out_prob.shape[0], -1, -1)
        
        assert (out_prob_exp >= 0).all() and (out_prob_exp <= 1).all(), "Invalid values in out_prob_exp"
        assert (tgt_ids_exp >= 0).all() and (tgt_ids_exp <= 1).all(), "Invalid values in tgt_ids_exp"
        # 但是我感觉啊，由于在获取特征的时候，本身我们就使用了text embed，所以本身来说，他们是天然就有高相似度的，几乎可以不用考虑这个的得分。
        losses = F.binary_cross_entropy(out_prob_exp, tgt_ids_exp, reduction="none")
        cost_class = losses.mean(dim=2) ## 算的是和text接近度最高的几个抽选作为目标结果。

        """ point cost """
        cost_point = torch.cdist(out_point, tgt_point, p=1) # (bs*nq, sum(n_b)) ## 算距离得分，最近的判断为目标。


        # cost
        sizes = [len(v["points"]) for v in targets]
        C = self.cost_point * cost_point + self.cost_class * cost_class
        C = C.view(bs, num_queries, -1).cpu()
        indices = [linear_sum_assignment(c[i]) for i, c in enumerate(C.split(sizes, -1))]

        return [(torch.as_tensor(i, dtype=torch.int64), torch.as_tensor(j, dtype=torch.int64)) for i, j in indices] # i: predicted, j: target



def sigmoid_focal_loss(
    inputs, targets, caption_sizes=None, alpha: float = 0.25, gamma: float = 2, no_reduction=False
):
    """
    Loss used in RetinaNet for dense detection: https://arxiv.org/abs/1708.02002.
    Args:
        inputs: A float tensor of arbitrary shape.
                The predictions for each example.
        targets: A float tensor with the same shape as inputs. Stores the binary
                 classification label for each element in inputs
                (0 for the negative class and 1 for the positive class).
        alpha: (optional) Weighting factor in range (0,1) to balance
                positive vs negative examples. Default = -1 (no weighting).
        gamma: Exponent of the modulating factor (1 - p_t) to
               balance easy vs hard examples.
    Returns:
        Loss tensor
    """

    prob = inputs.sigmoid() 
    ce_loss = F.binary_cross_entropy(prob, targets, reduction="none")

    p_t = prob * targets + (1 - prob) * (1 - targets)
    loss = ce_loss * ((1 - p_t) ** gamma)

    if alpha >= 0:
        alpha_t = alpha * targets + (1 - alpha) * (1 - targets)
        loss = alpha_t * loss

    if no_reduction:
        return loss
    else:

        loss_mean_batch = loss.mean(dim=1) # (bs,256)
        loss_mean_batch_sum = loss_mean_batch.sum(dim=1)/caption_sizes # (bs)
        loss_mean = loss_mean_batch_sum.mean()        

        return loss_mean

class SetCriterionCrossEntropy(nn.Module):
    def __init__(self):
        super().__init__()
        self.ce_loss = nn.CrossEntropyLoss()
        
    def forward(self, pred, gt):
        return self.ce_loss(pred, gt)

class L2Loss(nn.Module):
    def __init__(self, device='cpu', reduction='mean', downsampling_rate=1):
        super(L2Loss, self).__init__()
        self.reduction = reduction
        self.downsampling_rate = downsampling_rate
        self.kernel = torch.ones(1, 1, downsampling_rate, downsampling_rate).to(device)

    def forward(self, outputs, density_map):
        if self.downsampling_rate > 1:
            density_map = F.conv2d(density_map, self.kernel, stride=self.downsampling_rate)
        loss = F.mse_loss(outputs, density_map, reduction=self.reduction)
        return loss

class SmoothL1Loss(nn.Module):
    def __init__(self, device='cpu', reduction='mean', downsampling_rate=1):
        super(SmoothL1Loss, self).__init__()
        self.reduction = reduction
        self.downsampling_rate = downsampling_rate
        self.kernel = torch.ones(1, 1, downsampling_rate, downsampling_rate).to(device)

    def forward(self, outputs, density_map):
        if self.downsampling_rate > 1:
            density_map = F.conv2d(density_map, self.kernel, stride=self.downsampling_rate)
        loss = F.smooth_l1_loss(outputs, density_map, reduction=self.reduction)
        return loss



class SetCriterion(nn.Module):
    """ This class computes the loss for DETR.
        1) compute hungarian assignment between ground truth boxes and the outputs of the model
        2) supervise each pair of matched ground-truth / prediction (supervise class and localization)
    """
    def __init__(self):
        # Create the criterion.

        super().__init__()
        self.matcher = HungarianMatcher(cost_class=5, cost_point=1)
        self.losses = ['labels', 'points', 'contrast']
        self.weight_dict = {'loss_label': 5, 'loss_point': 1, 'loss_contrast': 0.06} 


    def loss_label(self, outputs, targets, indices, num_points, caption_sizes, **kwargs): 
        """Classification loss 
        targets dicts must contain the key "labels" containing a tensor of dim [nb_target_boxes]
        """
        assert 'pred_logits' in outputs
        src_logits = outputs['pred_logits'] 

        idx = self._get_src_permutation_idx(indices) # tuple ((b#1,b#1,b#1,b#1..., b#2,b#2,b#2...), (pred_idx, pred_idx, pred_idx...)) # 0, 1 size: sum(nt)
        target_classes_o = torch.cat([t["labels"][J] for t, (_, J) in zip(targets, indices)]) # (nt, 256) re-ordered in pred_idx order but using target labels
        target_classes = torch.full(src_logits.shape, 0.0,
                                    dtype=torch.int64, device=src_logits.device) # (bs, nq, 256)
        target_classes[idx] = target_classes_o.to(torch.int64) # set nt out of nq to target labels, others remain 0

        target_classes = target_classes.type_as(src_logits)
        caption_sizes = torch.cat([c.view(1) for c in caption_sizes], dim=0).to(device) 
        loss_label = sigmoid_focal_loss(src_logits, target_classes, caption_sizes=caption_sizes)


        losses = {'loss_label': loss_label}
        
        return losses

    def loss_point(self, outputs, targets, indices, num_points, **kwargs):
        
        assert 'pred_points' in outputs
        idx = self._get_src_permutation_idx(indices)
        src_points = outputs['pred_points'][idx] # (nt, 2)
        target_points = torch.cat([t['points'][i] for t, (_, i) in zip(targets, indices)], dim=0) # (nt, 2)

        loss_point = F.l1_loss(src_points, target_points, reduction='none')

        losses = {}
        losses['loss_point'] = loss_point.sum() / num_points
        
        return losses

    def loss_contrast(self, outputs, targets, indices, num_points, mask_bi=None, **kwargs):
        assert 'img_embs' in outputs and 'txt_embs' in outputs
        idx = self._get_src_permutation_idx(indices) 
        img_embs = outputs['img_embs'] 
        txt_embs = outputs['txt_embs'] 
        token_masks = outputs['token_masks']

        if img_embs.shape[0] == 1: # only one caption and one image in batch
            return {'loss_contrast': torch.zeros(1).to(device)}
        
        loss_list = []
        for i, img_emb in enumerate(img_embs): 
            bi = mask_bi[i] # index of image in the batch e.g [0,0,0,1,1,1]
            matched_pred_idxes = idx[1][torch.where(idx[0] == bi)[0]] # 得到对应的图像特征，实际上是初始化的embed的特征。
            matched_img_tokens = img_emb[matched_pred_idxes] 

            # positive text embedding
            pos_token_mask = token_masks[i].unsqueeze(0)  # (1, longest_cap) for the i-th img/caption
            pos_txt = txt_embs[i].unsqueeze(0)  # (1, longest_cap, 256)
            pos_txt_emb_sum = torch.sum(pos_txt * pos_token_mask.unsqueeze(-1), dim=1) 
            pos_txt_emb = pos_txt_emb_sum / pos_token_mask.sum(dim=1, keepdim=True) # (1,256)

            # negative text embedding
            idxes_for_img = np.where(np.array(mask_bi) == bi)[0]
            neg_idxes = [k for k in idxes_for_img if k != i]

            if len(neg_idxes) != 0:
                neg_token_masks = token_masks[neg_idxes] 
                neg_txts = txt_embs[neg_idxes]  
                neg_txt_embs_sum = torch.sum(neg_txts * neg_token_masks.unsqueeze(-1), dim=1)
                neg_txt_embs = neg_txt_embs_sum / neg_token_masks.sum(dim=1, keepdim=True) # (num_cap-1, 256)

                loss = self.contrastive_loss(matched_img_tokens, pos_txt_emb, neg_txt_embs)

            else: # only one caption for image
                loss = torch.tensor(0.0).to(device)
            
            loss_list.append(loss)
        
        loss_contrast = torch.stack(loss_list).mean()

        losses = {}
        losses['loss_contrast'] = loss_contrast
        
        return losses

    def contrastive_loss(self, img_embs, pos_txt_emb, neg_txt_embs):    
        img_embs = F.normalize(img_embs, p=2, dim=1)
        pos_txt_emb = F.normalize(pos_txt_emb, p=2, dim=1)
        neg_txt_embs = F.normalize(neg_txt_embs, p=2, dim=1)
        
        pos_sim = torch.mm(img_embs, pos_txt_emb.T)
        neg_sims = torch.mm(img_embs, neg_txt_embs.T)

        pos_labels = torch.ones(pos_sim.size()).to(pos_sim.device)
        neg_labels = torch.zeros(neg_sims.size()).to(neg_sims.device)

        pos_loss = F.binary_cross_entropy_with_logits(pos_sim, pos_labels)
        neg_loss = F.binary_cross_entropy_with_logits(neg_sims, neg_labels)

        total_loss = (pos_loss + neg_loss) / 2

        return total_loss

    def _get_src_permutation_idx(self, indices):
        # permute predictions following indices
        batch_idx = torch.cat([torch.full_like(src, i) for i, (src, _) in enumerate(indices)])
        src_idx = torch.cat([src for (src, _) in indices])
        return batch_idx, src_idx

    def _get_tgt_permutation_idx(self, indices):
        # permute targets following indices
        batch_idx = torch.cat([torch.full_like(tgt, i) for i, (_, tgt) in enumerate(indices)])
        tgt_idx = torch.cat([tgt for (_, tgt) in indices])
        return batch_idx, tgt_idx

    def get_loss(self, loss, outputs, targets, indices, num_points, **kwargs):
        loss_map = {
            'labels': self.loss_label,
            'points': self.loss_point,
            'contrast': self.loss_contrast,
        }
        assert loss in loss_map, f'do you really want to compute {loss} loss?'
        return loss_map[loss](outputs, targets, indices, num_points, **kwargs)

    def forward(self, outputs, targets, mask_bi=None):

        indices = self.matcher(outputs, targets) # list of tuple [(pred_index, target_index),(pred_index, target_index)]
        
        caption_sizes = [t['caption_size'] for t in targets] 
        bs, _ = outputs['pred_logits'].shape[:2] 

        
        num_points = sum(len(t["labels"]) for t in targets)
        num_points = torch.as_tensor([num_points], dtype=torch.float, device=next(iter(outputs.values())).device)
        num_points = torch.clamp(num_points / 1, min=1).item()

        losses = {}
        for loss in self.losses:
            losses.update(self.get_loss(loss, outputs, targets, indices, num_points, caption_sizes=caption_sizes, mask_bi=mask_bi))

        # re-arrange indices of outputs based on targets
        for b in range(bs):
            output_indices = indices[b][0] 
            target_indices = indices[b][1] 
            for ti, oi in zip(target_indices, output_indices):
                if ti > len(outputs['pred_points'][b]) - 1: 
                    continue
                temp = outputs['pred_points'][b][ti].clone() 
                outputs['pred_points'][b][ti] = outputs['pred_points'][b][oi]
                outputs['pred_points'][b][oi] = temp
                temp = outputs['pred_logits'][0][ti].clone()
                outputs['pred_logits'][b][ti] = outputs['pred_logits'][b][oi]
                outputs['pred_logits'][b][oi] = temp

        return losses


def extract_features_at_points(feature_map, points):
    """
    :param feature_map: 输入的图像特征，维度为 [c, h, w]
    :param points: 相对坐标点，维度为 [n, 2]，值在 [0, 1] 之间
    :return: 提取的特征向量，维度为 [n, c]
    """
    c, h, w = feature_map.shape
    
    # 将相对坐标转换为绝对坐标
    absolute_coords = points.clone()
    absolute_coords = torch.clamp(absolute_coords, min=0, max=1)
    absolute_coords[:, 0] = (points[:, 0] * (w - 1)).round()  # x 方向
    absolute_coords[:, 1] = (points[:, 1] * (h - 1)).round()  # y 方向
    
    absolute_coords = absolute_coords.long()
    # 提取每个点的特征向量
    feature_vectors = feature_map[:, absolute_coords[:, 1], absolute_coords[:, 0]].transpose(0, 1)
    
    return feature_vectors

class SetRegContrastiveCriterion(nn.Module):
    def __init__(self):
        super().__init__()
    
    ## TODO: 获得对应的图像特征，有多个层级。
    def loss_contrast(self, outputs, targets, mask_bi=None, **kwargs):
        assert 'encode_regression_feature' in outputs and 'txt_embs' in outputs
        img_embs_dict = outputs['encode_regression_feature'] 
        txt_embs = outputs['txt_embs'] 
        token_masks = outputs['token_masks']

        if img_embs_dict['vision_feature'].shape[0] == 1: # only one caption and one image in batch
            return torch.zeros(1).to(device)

        N_, S_, D_ = img_embs_dict['vision_feature'].shape
        _cur = 0
        img_embs = []
        for lvl, (H_, W_) in enumerate(img_embs_dict['spatial_shape']):
            # mask_flatten_ = mask_flatten[:, _cur : (_cur + H_ * W_)].view(N_, H_, W_, 1)
            img_emb = img_embs_dict['vision_feature'][:, _cur : (_cur + H_ * W_),:].view(N_, H_, W_, -1)
            img_embs.append(img_emb.permute(0,3,1,2))
            _cur += H_ * W_

        # 一个batch size一个batch size的计算
        ## 给定一个
        loss_list = []
        for l in range(len(img_embs)):
            for i, img_emb in enumerate(img_embs[l]):
                bi = mask_bi[i]
                matched_img_tokens = extract_features_at_points(img_emb, targets[i]['points'])
                # positive text embedding
                pos_token_mask = token_masks[i].unsqueeze(0)  # (1, longest_cap) for the i-th img/caption
                pos_txt = txt_embs[i].unsqueeze(0)  # (1, longest_cap, 256)
                pos_txt_emb_sum = torch.sum(pos_txt * pos_token_mask.unsqueeze(-1), dim=1) 
                pos_txt_emb = pos_txt_emb_sum / pos_token_mask.sum(dim=1, keepdim=True) # (1,256)

                # negative text embedding
                idxes_for_img = np.where(np.array(mask_bi) == bi)[0]
                neg_idxes = [k for k in idxes_for_img if k != i]

                if len(neg_idxes) != 0:
                    neg_token_masks = token_masks[neg_idxes] 
                    neg_txts = txt_embs[neg_idxes]  
                    neg_txt_embs_sum = torch.sum(neg_txts * neg_token_masks.unsqueeze(-1), dim=1)
                    neg_txt_embs = neg_txt_embs_sum / neg_token_masks.sum(dim=1, keepdim=True) # (num_cap-1, 256)

                    loss = self.contrastive_loss(matched_img_tokens, pos_txt_emb, neg_txt_embs)

                else: # only one caption for image
                    loss = torch.tensor(0.0).to(device)
                    
                loss_list.append(loss)

        loss_contrast = torch.stack(loss_list).mean()

        # losses = {}
        # losses['loss_contrast'] = loss_contrast
        
        return loss_contrast

    def contrastive_loss(self, img_embs, pos_txt_emb, neg_txt_embs):    
        img_embs = F.normalize(img_embs, p=2, dim=1)
        pos_txt_emb = F.normalize(pos_txt_emb, p=2, dim=1)
        neg_txt_embs = F.normalize(neg_txt_embs, p=2, dim=1)
        
        pos_sim = torch.mm(img_embs, pos_txt_emb.T)
        neg_sims = torch.mm(img_embs, neg_txt_embs.T)

        pos_labels = torch.ones(pos_sim.size()).to(pos_sim.device)
        neg_labels = torch.zeros(neg_sims.size()).to(neg_sims.device)

        pos_loss = F.binary_cross_entropy_with_logits(pos_sim, pos_labels)
        neg_loss = F.binary_cross_entropy_with_logits(neg_sims, neg_labels)

        total_loss = (pos_loss + neg_loss) / 2

        return total_loss
    
    def forward(self, outputs, targets, mask_bi=None):
        return self.loss_contrast(outputs, targets, mask_bi)
