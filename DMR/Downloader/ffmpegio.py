from datetime import datetime
import logging
import signal
import subprocess
import sys
import asyncio
import threading
import time
import queue
from os.path import *

from DMR.LiveAPI import Onair
from DMR.message import PipeMessage

class FFmpegDownloader():
    default_header = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'User-Agent': 'Mozilla/5.0 (Linux; Android 5.0; SM-G900P Build/LRX21T) AppleWebKit/537.36 '
                            '(KHTML, like Gecko) Chrome/75.0.3770.100 Mobile Safari/537.36 '
        }
    
    def __init__(self, 
                 stream_url:str, 
                 output:str, 
                 segment:int,
                 vid_format:str,
                 url:str,
                 taskname:str,
                 ffmpeg_stream_args:list,
                 ffmpeg:str,
                 debug=False,
                 header:dict=None,
                 **kwargs):
        self.stream_url = stream_url
        self.header = header if header else self.default_header
        self.segment = segment
        self.ffmpeg_stream_args = ffmpeg_stream_args
        self.debug = debug
        self.kwargs = kwargs
        self.output = f'{output}.{vid_format}'
        self.taskname = taskname
        self.url = url
        self.ffmpeg = ffmpeg
        self.stoped = False

    @property
    def duration(self):
        return datetime.now().timestamp() - self.starttime
        
    def start_ffmpeg(self):
        ffmpeg_args =   [
            self.ffmpeg, '-y',
            '-headers', ''.join('%s: %s\r\n' % x for x in self.header.items()),
            *self.ffmpeg_stream_args,
            '-i', self.stream_url,
            '-c','copy'
        ]
        
        if self.segment:
            ffmpeg_args += ['-f','segment',
                            '-segment_time',str(self.segment),
                            '-reset_timestamps','1',
                            '-movflags','faststart+frag_keyframe+empty_moov',
                            self.output]
        else:
            ffmpeg_args += ['-movflags','faststart+frag_keyframe+empty_moov',
                            self.output]

        
        logging.debug('FFmpegDownloader args:')
        logging.debug(ffmpeg_args)

        if self.debug:
            self.ffmpeg_proc = subprocess.Popen(ffmpeg_args, stdin=subprocess.PIPE, stdout=sys.stdout, stderr=subprocess.STDOUT,bufsize=10**8)
        else:
            self.ffmpeg_proc = subprocess.Popen(ffmpeg_args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,bufsize=10**8)
        
        self.msg_queue = None
        def ffmpeg_monitor():
            while not self.stoped:
                out = b''
                t0 = self.duration
                while not self.stoped:
                    if not self.ffmpeg_proc.stdout.readable():
                        break
                    char = self.ffmpeg_proc.stdout.read(1)
                    if char in [b'\n',b'\r',b'\0']:
                        break
                    elif self.duration-t0 > 10:
                        break
                    else:
                        out += char
                line = out.decode('utf-8',errors='ignore')
                if len(line) > 0:
                    self.msg_queue.put(line)
        
        if self.ffmpeg_proc.stdout is not None:
            self.msg_queue = queue.Queue()
            self.ffmpeg_monitor_proc = threading.Thread(target=ffmpeg_monitor, daemon=True)
            self.ffmpeg_monitor_proc.start()
        
        return self.msg_queue
    
    def start_helper(self):
        self.stoped = False
        self.starttime = datetime.now().timestamp()
        q = self.start_ffmpeg()
        
        log = ''
        ffmpeg_low_speed = 0
        self._timer_cnt = 1
        
        while not self.stoped:
            if q is None:
                time.sleep(1)
                continue
            
            try:
                line = self.msg_queue.get_nowait()
                log += line + '\n'
            except queue.Empty:
                time.sleep(1)
                    
            if self.ffmpeg_proc.poll() is not None:
                logging.debug('FFmpeg exit.')
                logging.debug(log)
                if Onair(self.url):
                    raise RuntimeError(f'FFmpeg 异常退出: {log}')

            if self.duration > self._timer_cnt*15:
                if len(log) == 0:
                    raise RuntimeError(f'{self.taskname} 管道读取错误, 即将重试.')
                
                err = 0
                for li in log.split('\n'):
                    if li and not li.startswith('frame='):
                        err = 1
                
                if err:
                    logging.debug(f'{self.taskname} FFmpeg output:\n{log}')
                else:
                    logging.debug(f'{self.taskname} FFmpeg output: ok.')

                if not self.kwargs.get('disable_lowspeed_interrupt'):
                    l = line.find('speed=')
                    r = line.find('x',l)
                    if l>0 and r>0:
                        speed = float(line[l:r][6:])
                        if speed < 0.9:
                            ffmpeg_low_speed += 1
                            logging.warn(f'{self.taskname} 直播流下载速度过慢, 请保证网络带宽充足.')
                            if ffmpeg_low_speed >= 2:
                                raise RuntimeError(f'{self.taskname} 下载速度过慢, 即将重试.')
                        else:
                            ffmpeg_low_speed = 0

                if 'dropping it' in log:
                    raise RuntimeError(f'{self.taskname} 直播流读取错误, 即将重试, 如果此问题多次出现请反馈.')

                if self._timer_cnt%3 == 0 and Onair(self.url) == False:
                    logging.debug('Live end.')
                    return

                log = ''
                self._timer_cnt += 1

    def start(self):
        thread = threading.Thread(target=self.start_helper,daemon=True)
        thread.start()
        return thread

    def stop(self):
        self.stoped = True
        try:
            out, _ = self.ffmpeg_proc.communicate(b'q',timeout=5)
            logging.debug(out)
        except subprocess.TimeoutExpired:
            try:
                self.ffmpeg_proc.send_signal(signal.SIGINT)
                out, _ = self.ffmpeg_proc.communicate()
                logging.debug(out)
            except Exception as e:
                logging.exception(e)
        except Exception as e:
            logging.exception(e)
        logging.debug('ffmpeg downloader stoped.')
