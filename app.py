import os
import cv2
import numpy as np
from datetime import datetime
import time
import argparse
import json
from pathlib import Path
from app_utils import RegionOfInterest

from tool.utils import *
from tool.torch_utils import do_detect
from tool.darknet2pytorch import Darknet

from deep_sort.deepsort import *
import torch
import ffmpeg

from waggle import plugin
from waggle.data.vision import VideoCapture, resolve_device
from waggle.data.timestamp import get_timestamp


class Yolov4Trck():
    def __init__(self, use_cuda, cfgfile='yolov4.cfg', weightfile='yolov4.weights'):
        self.m = Darknet(cfgfile)
        self.m.load_weights(weightfile)

        if use_cuda:
            self.m.cuda().eval()
        else:
            self.m.eval()

        self.use_cuda = use_cuda

    def run_yolov4(self, frame):
        sized = cv2.resize(frame, (512, 512))
        sized = cv2.cvtColor(sized, cv2.COLOR_BGR2RGB)

        #### Start detection using do_detect() function
#             start = time.time()
        ######### output must be boxes[0], which contains tbrl, confidence level, and class number
        boxes = do_detect(self.m, sized, 0.4, 0.6, self.use_cuda)
#             print(type(boxes), len(boxes[0]), ': number of detected cars', boxes[0])
#             finish = time.time()
#             print('yolo elapsed in: %f sec' % (finish - start))
        return boxes[0]


def call_deepsort(use_cuda, wt_path='model640.pt'):
    if use_cuda:
        m_deepsort = torch.load(wt_path)
        m_deepsort.cuda().eval()
    else:
        m_deepsort = torch.load(wt_path, map_location=torch.device('cpu'))
        m_deepsort.eval()

    return m_deepsort


class RunClass():
    def __init__(self, DSort, fps, labels):
        self.DSort = DSort
        self.flow = []
        self.occupancy_area = 0
        self.density_frames = 0
        self.speed = {}
        self.fps = fps
        self.class_names = load_class_names(labels)

    def set_roi(self, roi):
        self.roi = roi

    def clean_up(self):
        pass

    def calculate_flow(self, t, b, r, l, id_num):
        # Add id_num if the box (t, b, r, l) is entering the ROI
        ret = False
        if self.roi.overlaps(t, b, r, l):
            print(f'{id_num} touches the ROI')
            ret = True
            if id_num not in self.flow:
                self.flow.append(id_num)
        return ret

    def get_flow(self):
        return len(self.flow)

    def calculate_density(self, t, b, r, l, outclass):
        # Calculate occupancy area of the class and
        # accumulate the area
        name = self.class_names[outclass]
        #print(f'{name} recognized in {t}, {b}, {r}, {l}')
        if self.roi.contains(t, b, r, l):
            #print(f'{name} is contained inside the ROI')
            if 'car' in name:
                self.occupancy_area += 4.5 * 1.7
            elif 'bus' in name:
                self.occupancy_area += 13 * 2.55
            elif 'truck' in name:
                 self.occupancy_area += 5.5 * 2
        #self.density_frames += 1

    def get_occupancy(self):
        # Return the accumulated occupied area divided by the road area
        # and frames used for accumulating the occupied area
        print(f'area: {self.occupancy_area} road_area: {self.roi.road_area} density_frames: {self.density_frames}')
        return self.occupancy_area / self.roi.road_area / self.fps

    def calculate_speed(self, t, b, r, l, id_num):
        # Count frames while id_num touches or is in the ROI
        # If id_num was counted and no longer inside the ROI, then indicate it exited the ROI
        if self.roi.contains(t, b, r, l) or self.roi.overlaps(t, b, r, l):
            if id_num not in self.speed:
                self.speed[id_num] = 1
            else:
                self.speed[id_num] += 1
        else:
            if id_num in self.speed:
                self.speed[id_num] *= -1

    def get_averaged_speed(self):
        sum_speed = 0.
        for vehicle_id, counted_frames in self.speed.items():
            # negative frames mean the vehicle exited the ROI
            # so considerable to calculate the speed because it means
            # the vehicle traveled the distance of the ROI within the counted frames
            if counted_frames < 0:
                delta_t = -1 * counted_frames / self.fps
                delta_d = self.roi.road_area
                sum_speed += delta_d / delta_t * 3.6 # m/s to km/h
        return 0. if len(self.spped.keys()) == 0 else sum_speed / len(self.speed.keys())

    def reset_flow_and_occupancy(self):
        self.flow = []
        self.occupancy_area = 0.
        self.density_frames = 0

    def reset_speed(self):
        self.speed = {}

    def run_dsort(self, boxes, frame):
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        frame = frame.astype(np.uint8)

        tracker, detections_class = self.DSort.a_run_deep_sort(frame, boxes)

        for track in tracker.tracks:
#                 print('track.is_confirmed(): ', track.is_confirmed())
#                 print('track.time_since_update: ', track.time_since_update)
            if not track.is_confirmed() or track.time_since_update > 1:
                continue

            bbox = track.to_tlbr() #Get the corrected/predicted bounding box
            id_num = str(track.track_id) #Get the ID for the particular track.
            features = track.features #Get the feature vector corresponding to the detection.

            l = bbox[0]  ## x1
            t = bbox[1]  ## y1
            r = bbox[2]  ## x2
            b = bbox[3]  ## y2
            frame = cv2.putText(frame, id_num, (int(l), int(t)-10), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0,255,0), 2)

            if self.calculate_flow(t, b, r, l, id_num):
                frame = cv2.rectangle(frame, (int(l), int(t)), (int(r), int(b)), (255, 0, 0), 2)
            else:
                frame = cv2.rectangle(frame, (int(l), int(t)), (int(r), int(b)), (0, 255, 0), 2)

            self.calculate_density(t, b, r, l, track.outclass)
            self.calculate_speed(t, b, r, l, id_num)
        return cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)


def configure_and_load_models(args):
    device_url = resolve_device(Path(args.stream))
    cap = cv2.VideoCapture(args.stream)
    cvfps = args.fps
    width  = cap.get(cv2.CAP_PROP_FRAME_WIDTH)  # float
    height = cap.get(cv2.CAP_PROP_FRAME_HEIGHT) # float
    cap.release()
    print(f'Input stream {device_url} with size of W: {width}, H: {height}')

    use_cuda = False if args.no_cuda else True
    print(f'CUDA: {use_cuda}')
    
    print('Loading models...')
    o_detect = Yolov4Trck(use_cuda=use_cuda)

    #### deepsort model
    m_deepsort = call_deepsort(use_cuda=use_cuda)
    DSort = deepsort_rbc(m_deepsort, width, height, use_cuda=use_cuda)
    r_class = RunClass(DSort, fps=cvfps, labels=args.labels)
    print('Done')

    print('Configuring target area...')
    #ret, config_filename = load_and_generate_config(args.config)
    with open(args.config, 'r') as file:
        loaded_config = json.load(file)
    roi = RegionOfInterest(
        loaded_config['regionofinterest'],
        width,
        height,
        loaded_config['road_area'])
    print(f'Boundary of the ROI: {roi.roi.bounds}')
    r_class.set_roi(roi)
    return o_detect, r_class


"""
    ffmpeg describes that 
    https://trac.ffmpeg.org/wiki/ChangingFrameRate
"""
def take_sample(stream, duration, skip_second, resampling, resampling_fps):
    stream_url = resolve_device(stream)
    # Assume PyWaggle's timestamp is in nano seconds
    timestamp = get_timestamp() + skip_second * 1e9
    try:
        script_dir = os.path.dirname(__file__)
    except NameError:
        script_dir = os.getcwd()
    filename_raw = os.path.join(script_dir, 'sample_raw.mp4')
    filename = os.path.join(script_dir, 'sample.mp4')

    c = ffmpeg.input(stream_url, ss=skip_second).output(
        filename_raw,
        codec = "copy", # use same codecs of the original video
        f='mp4',
        t=duration).overwrite_output()
    print(c.compile())
    c.run(quiet=True)

    d = ffmpeg.input(filename_raw)
    if resampling:
        print(f'Resampling to {resampling_fps}...')
        d = ffmpeg.filter(d, 'fps', fps=resampling_fps)
    d = ffmpeg.output(d, filename, f='mp4', t=duration).overwrite_output()
    print(d.compile())
    d.run(quiet=True)
    # TODO: We may want to inspect whether the ffmpeg commands succeeded
    return True, filename, timestamp


# Deprecated!
# def record_video(stream, duration=10, fps=12):
#     with VideoCapture(stream) as cap:
#         width  = cap.get(cv2.CAP_PROP_FRAME_WIDTH)  # float
#         height = cap.get(cv2.CAP_PROP_FRAME_HEIGHT) # float

#         fourcc = cv2.VideoWriter_fourcc(*'mp4v')
#         filename = "record.mp4"
#         out = cv2.VideoWriter(filename, fourcc, fps, (int(width),int(height)), True)

#         start_time = time.time()
#         err = ""
#         timestamp = get_timestamp()
#         captured_frames = 0
#         while captured_frames <= (duration * fps):
#             ok, frame = cap.read()
#             if not ok:
#                 err = "Failed to capture a frame"
#                 break
#             out.write(frame)
#             captured_frames += 1
#         out.release()
#         print(f'{captured_frames} captured')
#         return err, timestamp, filename, width, height


def run(args):
    print('Loading and configuring models...')
    o_detect, r_class = configure_and_load_models(args)
    print('Done')

    sampling_countdown = -1
    if args.sampling_interval > -1:
        print(f'Input video will be sampled every {args.sampling_interval}th inferencing')
        sampling_countdown = args.sampling_interval

    print('Starting traffic state estimation..')
    plugin.init()
    while True:
        print(f'Grabbing video for {args.duration} seconds')
        ret, filename, timestamp = take_sample(
            stream=args.stream,
            duration=args.duration,
            skip_second=args.skip_second,
            resampling=args.resampling,
            resampling_fps=args.resampling_fps
        )
        if ret == False:
            print('Coud not sample video. Exiting...')
            return 1

        print('Analyzing the video...')
        total_frames = 0
        do_sampling = False
        if sampling_countdown > 0:
            sampling_countdown -= 1
        elif sampling_countdown == 0:
            do_sampling = True
            sampling_countdown = args.sampling_interval

        with VideoCapture(filename) as cap:
            width  = cap.get(cv2.CAP_PROP_FRAME_WIDTH)  # float
            height = cap.get(cv2.CAP_PROP_FRAME_HEIGHT) # float

            if do_sampling:
                b = r_class.roi.roi.bounds
                fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                out = cv2.VideoWriter("sample.mp4", fourcc, args.fps, (int(width), int(height)), True)

            while True:
                ret, frame = cap.read()
                if ret == False:
                    break

                result = o_detect.run_yolov4(frame)
                sample = r_class.run_dsort(result, frame)
                if do_sampling:
                    sample = cv2.rectangle(sample, (int(b[0]), int(b[1])), (int(b[2]), int(b[3])), (255, 0, 0), 2)
                    out.write(sample)
                total_frames += 1

                if total_frames % args.fps == 0:
                    elapsed_time = timestamp + int((total_frames / args.fps)) * 1e9
                    ##### traffic occupancy
                    occupancy = r_class.get_occupancy()
                    plugin.publish(
                        'traffic.state.occupancy',
                        occupancy,
                        timestamp=elapsed_time)

                    ##### traffic flow
                    flow = r_class.get_flow()
                    plugin.publish(
                        'traffic.state.flow', 
                        flow,
                        timestamp=elapsed_time)
                    print(f'{datetime.fromtimestamp(elapsed_time / 1.e9)} Traffic occupancy: {occupancy} flow: {flow}')
                    # Reset the accumulated values
                    r_class.reset_flow_and_occupancy()

            ##### traffic speed
            averaged_speed = r_class.get_averaged_speed()
            plugin.publish(
                'traffic.state.averaged_speed',
                averaged_speed,
                timestamp=timestamp)
            print(f'{datetime.fromtimestamp(timestamp / 1.e9)} Traffic speed: {averaged_speed}')
        if do_sampling:
            out.release()
            plugin.upload_file("sample.mp4")
        r_class.clean_up()
        print('Tracker is cleaned up for next analysis')


if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-no-cuda', dest='no_cuda',
        action='store_true', help="Do not use CUDA")
    parser.add_argument(
        '-stream', dest='stream',
        action='store', default="camera", type=str,
        help='ID or name of a stream, e.g. sample')
    parser.add_argument(
        '-duration', dest='duration',
        action='store', default=10., type=float,
        help='Time duration for input video')
    parser.add_argument(
        '-resampling', dest='resampling', default=False,
        action='store_true', help="Resampling the sample to -resample-fps option (defualt 12)")
    parser.add_argument(
        '-resampling-fps', dest='resampling_fps',
        action='store', default=12, type=int,
        help='Frames per second for input video')
    parser.add_argument(
        '-labels', dest='labels',
        action='store', default='detection/coco.names', type=str,
        help='Labels for detection')
    parser.add_argument(
        '-roi-name', dest='roi_name',
        action='store', type=str,
        help='Name of RoI used when publishing data')
    parser.add_argument(
        '-roi-coordinates', dest='roi_coordinates',
        action='store', type=str,
        help='Coordinates of RoI with X,Ys in relative values of (0. - 1.)')
    parser.add_argument(
        '-sampling-interval', dest='sampling_interval',
        action='store', default=-1, type=int,
        help='Inferencing interval for sampling results')
    args = parser.parse_args()
    run(args)
