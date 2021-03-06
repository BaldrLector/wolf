import os
import sys

current_path = os.path.dirname(os.path.realpath(__file__))
root_path = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.append(root_path)

import signal
import threading
import torch

import experiments.options as options
from experiments.train import main as single_process_main


def main():
    args = options.parse_distributed_args()
    args_dict = vars(args)

    nproc_per_node = args_dict.pop('nproc_per_node')
    nnodes = args_dict.pop('nnodes')
    node_rank = args_dict.pop('node_rank')

    # world size in terms of number of processes
    dist_world_size = nproc_per_node * nnodes

    # set PyTorch distributed related environmental variables
    current_env = os.environ
    current_env["MASTER_ADDR"] = args_dict.pop('master_addr')
    current_env["MASTER_PORT"] = str(args_dict.pop('master_port'))
    current_env["WORLD_SIZE"] = str(dist_world_size)

    batch_size = args.batch_size // dist_world_size
    args.batch_size = batch_size

    mp = torch.multiprocessing.get_context('spawn')
    # Create a thread to listen for errors in the child processes.
    error_queue = mp.SimpleQueue()
    error_handler = ErrorHandler(error_queue)

    processes = []

    for local_rank in range(0, nproc_per_node):
        # each process's rank
        dist_rank = nproc_per_node * node_rank + local_rank
        args.rank = dist_rank
        args.local_rank = local_rank
        process = mp.Process(target=run, args=(args, error_queue, ), daemon=False)
        process.start()
        error_handler.add_child(process.pid)
        processes.append(process)

    for process in processes:
        process.join()


def run(args, error_queue):
    try:
        single_process_main(args)
    except KeyboardInterrupt:
        pass  # killed by parent, do nothing
    except Exception:
        # propagate exception to parent process, keeping original traceback
        import traceback
        error_queue.put((args.rank, traceback.format_exc()))


class ErrorHandler(object):
    """A class that listens for exceptions in children processes and propagates
    the tracebacks to the parent process."""

    def __init__(self, error_queue):
        self.error_queue = error_queue
        self.children_pids = []
        self.error_thread = threading.Thread(target=self.error_listener, daemon=True)
        self.error_thread.start()
        signal.signal(signal.SIGUSR1, self.signal_handler)

    def add_child(self, pid):
        self.children_pids.append(pid)

    def error_listener(self):
        (rank, original_trace) = self.error_queue.get()
        self.error_queue.put((rank, original_trace))
        os.kill(os.getpid(), signal.SIGUSR1)

    def signal_handler(self, signalnum, stackframe):
        for pid in self.children_pids:
            os.kill(pid, signal.SIGINT)  # kill children processes
        (rank, original_trace) = self.error_queue.get()
        msg = "\n\n-- Tracebacks above this line can probably be ignored --\n\n"
        msg += original_trace
        raise Exception(msg)


if __name__ == "__main__":
    main()
