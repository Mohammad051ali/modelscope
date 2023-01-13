#!/usr/bin/env python
# Copyright (c) Alibaba, Inc. and its affiliates.

import copy
import os
import pickle
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import unittest
from collections import OrderedDict

import requests
import torch
from torch.utils.data import Dataset

from modelscope.utils.import_utils import is_tf_available, is_torch_available
from modelscope.utils.torch_utils import _find_free_port

TEST_LEVEL = 2
TEST_LEVEL_STR = 'TEST_LEVEL'


def test_level():
    global TEST_LEVEL
    if TEST_LEVEL_STR in os.environ:
        TEST_LEVEL = int(os.environ[TEST_LEVEL_STR])

    return TEST_LEVEL


def require_tf(test_case):
    if not is_tf_available():
        test_case = unittest.skip('test requires TensorFlow')(test_case)
    return test_case


def require_torch(test_case):
    if not is_torch_available():
        test_case = unittest.skip('test requires PyTorch')(test_case)
    return test_case


def set_test_level(level: int):
    global TEST_LEVEL
    TEST_LEVEL = level


class DummyTorchDataset(Dataset):

    def __init__(self, feat, label, num) -> None:
        self.feat = feat
        self.label = label
        self.num = num

    def __getitem__(self, index):
        return {
            'feat': torch.Tensor(self.feat),
            'labels': torch.Tensor(self.label)
        }

    def __len__(self):
        return self.num


def create_dummy_test_dataset(feat, label, num):
    return DummyTorchDataset(feat, label, num)


def download_and_untar(fpath, furl, dst) -> str:
    if not os.path.exists(fpath):
        r = requests.get(furl)
        with open(fpath, 'wb') as f:
            f.write(r.content)

    file_name = os.path.basename(fpath)
    root_dir = os.path.dirname(fpath)
    target_dir_name = os.path.splitext(os.path.splitext(file_name)[0])[0]
    target_dir_path = os.path.join(root_dir, target_dir_name)

    # untar the file
    t = tarfile.open(fpath)
    t.extractall(path=dst)

    return target_dir_path


def get_case_model_info():
    status_code, result = subprocess.getstatusoutput(
        'grep -rn "damo/" tests/  | grep -v ".pyc" | grep -v "Binary file" | grep -v run.py '
    )
    lines = result.split('\n')
    test_cases = OrderedDict()
    model_cases = OrderedDict()
    for line in lines:
        # "tests/msdatasets/test_ms_dataset.py:92:        model_id = 'damo/bert-base-sst2'"
        line = line.strip()
        elements = line.split(':')
        test_file = elements[0]
        model_pos = line.find('damo')
        left_quote = line[model_pos - 1]
        rquote_idx = line.rfind(left_quote)
        model_name = line[model_pos:rquote_idx]
        if test_file not in test_cases:
            test_cases[test_file] = set()
        model_info = test_cases[test_file]
        model_info.add(model_name)

        if model_name not in model_cases:
            model_cases[model_name] = set()
        case_info = model_cases[model_name]
        case_info.add(
            test_file.replace('tests/', '').replace('.py',
                                                    '').replace('/', '.'))

    return model_cases


_DIST_SCRIPT_TEMPLATE = """
import ast
import argparse
import pickle
import torch
from torch import distributed as dist
from modelscope.utils.torch_utils import get_dist_info
import {}

parser = argparse.ArgumentParser()
parser.add_argument('--save_all_ranks', type=ast.literal_eval, help='save all ranks results')
parser.add_argument('--save_file', type=str, help='save file')
parser.add_argument('--local_rank', type=int, default=0)
args = parser.parse_args()


def main():
    results = {}.{}({})  # module.func(params)
    if args.save_all_ranks:
        save_file = args.save_file + str(dist.get_rank())
        with open(save_file, 'wb') as f:
            pickle.dump(results, f)
    else:
        rank, _ = get_dist_info()
        if rank == 0:
            with open(args.save_file, 'wb') as f:
                pickle.dump(results, f)


if __name__ == '__main__':
    main()
"""


class DistributedTestCase(unittest.TestCase):
    """Distributed TestCase for test function with distributed mode.
    Examples:
        import torch
        from torch import distributed as dist
        from modelscope.utils.torch_utils import init_dist

        def _test_func(*args, **kwargs):
            init_dist(launcher='pytorch')
            rank = dist.get_rank()
            if rank == 0:
                value = torch.tensor(1.0).cuda()
            else:
                value = torch.tensor(2.0).cuda()
            dist.all_reduce(value)
            return value.cpu().numpy()

        class DistTest(DistributedTestCase):
            def test_function_dist(self):
                args = ()  # args should be python builtin type
                kwargs = {}  # kwargs should be python builtin type
                self.start(
                    _test_func,
                    num_gpus=2,
                    assert_callback=lambda x: self.assertEqual(x, 3.0),
                    *args,
                    **kwargs,
                )
    """

    def _start(self,
               dist_start_cmd,
               func,
               num_gpus,
               assert_callback=None,
               save_all_ranks=False,
               *args,
               **kwargs):
        script_path = func.__code__.co_filename
        script_dir, script_name = os.path.split(script_path)
        script_name = os.path.splitext(script_name)[0]
        func_name = func.__qualname__

        func_params = []
        for arg in args:
            if isinstance(arg, str):
                arg = ('\'{}\''.format(arg))
            func_params.append(str(arg))

        for k, v in kwargs.items():
            if isinstance(v, str):
                v = ('\'{}\''.format(v))
            func_params.append('{}={}'.format(k, v))

        func_params = ','.join(func_params).strip(',')

        tmp_run_file = tempfile.NamedTemporaryFile(suffix='.py').name
        tmp_res_file = tempfile.NamedTemporaryFile(suffix='.pkl').name

        with open(tmp_run_file, 'w') as f:
            print('save temporary run file to : {}'.format(tmp_run_file))
            print('save results to : {}'.format(tmp_res_file))
            run_file_content = _DIST_SCRIPT_TEMPLATE.format(
                script_name, script_name, func_name, func_params)
            f.write(run_file_content)

        tmp_res_files = []
        if save_all_ranks:
            for i in range(num_gpus):
                tmp_res_files.append(tmp_res_file + str(i))
        else:
            tmp_res_files = [tmp_res_file]
        self.addCleanup(self.clean_tmp, [tmp_run_file] + tmp_res_files)

        tmp_env = copy.deepcopy(os.environ)
        tmp_env['PYTHONPATH'] = ':'.join(
            (tmp_env.get('PYTHONPATH', ''), script_dir)).lstrip(':')
        # avoid distributed test hang
        tmp_env['NCCL_P2P_DISABLE'] = '1'
        script_params = '--save_all_ranks=%s --save_file=%s' % (save_all_ranks,
                                                                tmp_res_file)
        script_cmd = '%s %s %s' % (dist_start_cmd, tmp_run_file, script_params)
        print('script command: %s' % script_cmd)
        res = subprocess.call(script_cmd, shell=True, env=tmp_env)

        script_res = []
        for res_file in tmp_res_files:
            with open(res_file, 'rb') as f:
                script_res.append(pickle.load(f))
        if not save_all_ranks:
            script_res = script_res[0]

        if assert_callback:
            assert_callback(script_res)

        self.assertEqual(
            res,
            0,
            msg='The test function ``{}`` in ``{}`` run failed!'.format(
                func_name, script_name))

        return script_res

    def start(self,
              func,
              num_gpus,
              assert_callback=None,
              save_all_ranks=False,
              *args,
              **kwargs):
        ip = socket.gethostbyname(socket.gethostname())
        dist_start_cmd = '%s -m torch.distributed.launch --nproc_per_node=%d --master_addr=\'%s\' --master_port=%s' % (
            sys.executable, num_gpus, ip, _find_free_port())

        return self._start(
            dist_start_cmd=dist_start_cmd,
            func=func,
            num_gpus=num_gpus,
            assert_callback=assert_callback,
            save_all_ranks=save_all_ranks,
            *args,
            **kwargs)

    def clean_tmp(self, tmp_file_list):
        for file in tmp_file_list:
            if os.path.exists(file):
                if os.path.isdir(file):
                    shutil.rmtree(file)
                else:
                    os.remove(file)
