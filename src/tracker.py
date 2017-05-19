
import tensorflow as tf
assert tf.__version__>='1.0.0', ('You should use Tensorflow 1.0 or superior')
from PIL import Image
import matplotlib.pyplot as plt
import sys
import os.path
import csv
import numpy as np
import time

import src.siamese as siam
from src.parse_params import parse_params
from src.region_to_bbox import region_to_bbox
from src.pprint_params import pprint_params
from src.visualization import show_frame, show_crops, show_scores


def tracker(hp, evaluation, run):
    # overwrite default parameters with ones passed 
    hp,evaluation,run,env,design = parse_params(hp, evaluation, run)

    video_folder = os.path.join(env.root_dataset, evaluation.dataset, evaluation.video)
    frame_name_list = [f for f in os.listdir(video_folder) if f.endswith(".jpg")]
    frame_name_list = [os.path.join(env.root_dataset, evaluation.dataset, evaluation.video, '') + s for s in frame_name_list]
    frame_name_list.sort()
    num_frames = np.size(frame_name_list)

    with Image.open(frame_name_list[0]) as img:
        frame_sz = np.asarray(img.size)
        frame_sz[1], frame_sz[0] = frame_sz[0], frame_sz[1]

    # read the initialization from ground truth
    gt_file = os.path.join(video_folder, 'groundtruth.txt')
    gt = np.genfromtxt(gt_file, delimiter=',')
    assert len(gt) == len(frame_name_list), ('Number of frames and number of GT lines should be equal.')
    # stores tracker's output for evaluation
    bboxes = np.zeros((num_frames,4))

    scale_factors = hp.scale_step**np.linspace(-np.ceil(hp.scale_num/2), np.ceil(hp.scale_num/2), hp.scale_num)
    # cosine window to penalize large displacements
    final_score_sz = int(hp.response_up * design.score_sz)
    hann_1d = np.expand_dims(np.hanning(final_score_sz), axis=0)
    penalty = np.transpose(hann_1d) * hann_1d
    penalty = penalty / np.sum(penalty)

    ## tracker's state initializations
    # bbox is in format <cx,cy,w,h>
    pos_x,pos_y,target_w,target_h = region_to_bbox(gt[evaluation.start_frame])
    context = design.context*(target_w+target_h)
    z_sz = np.sqrt(np.prod((target_w+context)*(target_h+context)))
    x_sz = design.search_sz/design.exemplar_sz * z_sz

    # thresholds to saturate patches shrinking/growing
    min_z = hp.scale_min * z_sz
    max_z = hp.scale_max * z_sz
    min_x = hp.scale_min * x_sz
    max_x = hp.scale_max * x_sz

    #### BUILD THE GRAPH #####
    image, template_z, scores = siam.build_tracking_graph(frame_name_list, num_frames, frame_sz, final_score_sz, design, env)
    ####

    #### START TRACKING WITHIN A TF SESSION ####
    
    with tf.Session() as sess:
        tf.global_variables_initializer().run()
        # Coordinate the loading of image files.
        coord = tf.train.Coordinator()
        threads = tf.train.start_queue_runners(coord=coord)
        
        # save first frame position (from ground-truth)
        bboxes[0,:] = pos_x-target_w/2, pos_y-target_h/2, target_w, target_h
        
        image_, template_z_ = sess.run([image, template_z], feed_dict={siam.pos_x_ph:pos_x, siam.pos_y_ph:pos_y, siam.z_sz_ph:z_sz})
        
        if run.visualization:
            show_frame(image_, bboxes[0,:], 1)
            
        if run.debug:
            show_crops(z_crops_, 2)        

        t_start = time.time()
        # Get an image from the queue
        for i in xrange(evaluation.start_frame+1, num_frames):        
            scaled_exemplar = z_sz * scale_factors
            scaled_search_area = x_sz * scale_factors
            scaled_target_w = target_w * scale_factors
            scaled_target_h = target_h * scale_factors
                    
            scores_ = sess.run([scores], feed_dict={
                                            siam.pos_x_ph:pos_x,
                                            siam.pos_y_ph:pos_y,
                                            siam.x_sz0_ph:scaled_search_area[0],
                                            siam.x_sz1_ph:scaled_search_area[1],
                                            siam.x_sz2_ph:scaled_search_area[2],
                                            template_z: template_z_})                
            if run.debug:
                show_crops(np.squeeze(x_crops_), 3)
                show_scores(np.squeeze(scores_), 4)
                
            scores_ = np.squeeze(scores_)
            # penalize change of scale
            scores_[0,:,:] = hp.scale_penalty*scores_[0,:,:]
            scores_[2,:,:] = hp.scale_penalty*scores_[2,:,:]
            # find scale with highest peak (after penalty)
            new_scale_id = np.argmax(np.amax(scores_, axis=(1,2)))
            # update scaled sizes
            x_sz = (1-hp.scale_lr)*x_sz + hp.scale_lr*scaled_search_area[new_scale_id]        
            target_w = (1-hp.scale_lr)*target_w + hp.scale_lr*scaled_target_w[new_scale_id]
            target_h = (1-hp.scale_lr)*target_h + hp.scale_lr*scaled_target_h[new_scale_id]
            # select response with new_scale_id
            score_ = scores_[new_scale_id,:,:]
            score_ = score_ - np.min(score_)
            # apply displacement penalty
            score_ = (1-hp.window_influence)*score_ + hp.window_influence*penalty
            # find location of score maximizer
            p = np.asarray(np.unravel_index(np.argmax(score_), np.shape(score_)))
            # displacement from the center in search area final representation ...
            disp_in_area = p - float(final_score_sz)/2
            # displacement from the center in instance crop
            disp_in_xcrop = disp_in_area * float(design.tot_stride) / hp.response_up
            # displacement from the center in instance crop (in frame coordinates)
            disp_in_frame = disp_in_xcrop *  x_sz / design.search_sz
            # *position* within frame in frame coordinates
            pos_y, pos_x = pos_y + disp_in_frame[0], pos_x + disp_in_frame[1]
            # convert <cx,cy,w,h> to <x,y,w,h> and save output
            bboxes[i,:] = pos_x-target_w/2, pos_y-target_h/2, target_w, target_h
            print '('+str(bboxes[i,0])+', '+str(bboxes[i,1])+', '+str(bboxes[i,2])+', '+str(bboxes[i,3])+')'
            
            # update the target representation with a rolling average
            if hp.z_lr>0:
                new_template_z_ = sess.run([template_z], feed_dict={siam.pos_x_ph:pos_x, siam.pos_y_ph:pos_y, siam.z_sz_ph:z_sz})
                template_z_=(1-hp.z_lr)*template_z_ + hp.z_lr*np.squeeze(new_template_z_)
            
            # update template patch size
            z_sz = (1-hp.scale_lr)*z_sz + hp.scale_lr*scaled_exemplar[new_scale_id]
            
            if run.visualization:
                show_frame(image_, bboxes[i,:], 1)        

        t_elapsed = time.time() - t_start
        speed = (num_frames-evaluation.start_frame+1)/t_elapsed
        print 'Speed: '+str(speed)
        # Finish off the filename queue coordinator.
        coord.request_stop()
        coord.join(threads)