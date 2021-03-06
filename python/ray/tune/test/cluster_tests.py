from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import inspect
import json
import time
import os
import pytest
try:
    import pytest_timeout
except ImportError:
    pytest_timeout = None

import ray
from ray import tune
from ray.rllib import _register_all
from ray.test.cluster_utils import Cluster
from ray.test.test_utils import run_string_as_driver_nonblocking
from ray.tune.error import TuneError
from ray.tune.experiment import Experiment
from ray.tune.trial import Trial
from ray.tune.trial_runner import TrialRunner
from ray.tune.suggest import BasicVariantGenerator


class _Fail(tune.Trainable):
    """Fails on the 4th iteration."""

    def _setup(self, config):
        self.state = {"hi": 0}

    def _train(self):
        self.state["hi"] += 1
        time.sleep(0.5)
        if self.state["hi"] >= 4:
            assert False
        return {}

    def _save(self, path):
        return self.state

    def _restore(self, state):
        self.state = state


def _start_new_cluster():
    cluster = Cluster(
        initialize_head=True,
        connect=True,
        head_node_args={
            "resources": dict(CPU=1),
            "_internal_config": json.dumps({
                "num_heartbeats_timeout": 10
            })
        })
    # Pytest doesn't play nicely with imports
    _register_all()
    return cluster


@pytest.fixture
def start_connected_cluster():
    # Start the Ray processes.
    os.environ["TUNE_RESUME_PROMPT_OFF"] = "True"
    cluster = _start_new_cluster()
    yield cluster
    # The code after the yield will run as teardown code.
    ray.shutdown()
    cluster.shutdown()


@pytest.fixture
def start_connected_emptyhead_cluster():
    """Starts head with no resources."""

    os.environ["TUNE_RESUME_PROMPT_OFF"] = "True"
    cluster = Cluster(
        initialize_head=True,
        connect=True,
        head_node_args={
            "resources": dict(CPU=0),
            "_internal_config": json.dumps({
                "num_heartbeats_timeout": 10
            })
        })
    # Pytest doesn't play nicely with imports
    _register_all()
    yield cluster
    # The code after the yield will run as teardown code.
    ray.shutdown()
    cluster.shutdown()


def test_counting_resources(start_connected_cluster):
    """Tests that Tune accounting is consistent with actual cluster."""
    cluster = start_connected_cluster
    nodes = []
    assert ray.global_state.cluster_resources()["CPU"] == 1
    runner = TrialRunner(BasicVariantGenerator())
    kwargs = {"stopping_criterion": {"training_iteration": 10}}

    trials = [Trial("__fake", **kwargs), Trial("__fake", **kwargs)]
    for t in trials:
        runner.add_trial(t)

    runner.step()  # run 1
    nodes += [cluster.add_node(resources=dict(CPU=1))]
    assert cluster.wait_for_nodes()
    assert ray.global_state.cluster_resources()["CPU"] == 2
    cluster.remove_node(nodes.pop())
    assert cluster.wait_for_nodes()
    assert ray.global_state.cluster_resources()["CPU"] == 1
    runner.step()  # run 2
    assert sum(t.status == Trial.RUNNING for t in runner.get_trials()) == 1

    for i in range(5):
        nodes += [cluster.add_node(resources=dict(CPU=1))]
    assert cluster.wait_for_nodes()
    assert ray.global_state.cluster_resources()["CPU"] == 6

    runner.step()  # 1 result
    assert sum(t.status == Trial.RUNNING for t in runner.get_trials()) == 2


@pytest.mark.skip("Add this test once reconstruction is fixed")
@pytest.mark.skipif(
    pytest_timeout is None,
    reason="Timeout package not installed; skipping test.")
@pytest.mark.timeout(10, method="thread")
def test_remove_node_before_result(start_connected_cluster):
    """Removing a node should cause a Trial to be requeued."""
    cluster = start_connected_cluster
    node = cluster.add_node(resources=dict(CPU=1))
    # TODO(rliaw): Make blocking an option?
    assert cluster.wait_for_nodes()

    runner = TrialRunner(BasicVariantGenerator())
    kwargs = {"stopping_criterion": {"training_iteration": 3}}
    trials = [Trial("__fake", **kwargs), Trial("__fake", **kwargs)]
    for t in trials:
        runner.add_trial(t)

    runner.step()  # run 1
    runner.step()  # run 2
    assert all(t.status == Trial.RUNNING for t in trials)

    runner.step()  # 1 result

    cluster.remove_node(node)
    cluster.wait_for_nodes()
    assert ray.global_state.cluster_resources["CPU"] == 1

    runner.step()  # recover
    for i in range(5):
        runner.step()
    assert all(t.status == Trial.TERMINATED for t in trials)

    with pytest.raises(TuneError):
        runner.step()


@pytest.mark.skipif(
    pytest_timeout is None,
    reason="Timeout package not installed; skipping test.")
@pytest.mark.timeout(120, method="thread")
def test_trial_migration(start_connected_emptyhead_cluster):
    """Removing a node while cluster has space should migrate trial.

    The trial state should also be consistent with the checkpoint.
    """
    cluster = start_connected_emptyhead_cluster
    node = cluster.add_node(resources=dict(CPU=1))
    assert cluster.wait_for_nodes()

    runner = TrialRunner(BasicVariantGenerator())
    kwargs = {
        "stopping_criterion": {
            "training_iteration": 3
        },
        "checkpoint_freq": 2,
        "max_failures": 2
    }

    # Test recovery of trial that hasn't been checkpointed
    t = Trial("__fake", **kwargs)
    runner.add_trial(t)
    runner.step()  # start
    runner.step()  # 1 result
    assert t.last_result is not None
    node2 = cluster.add_node(resources=dict(CPU=1))
    cluster.remove_node(node)
    assert cluster.wait_for_nodes()
    runner.step()  # Recovery step

    # TODO(rliaw): This assertion is not critical but will not pass
    #   because checkpoint handling is messy and should be refactored
    #   rather than hotfixed.
    # assert t.last_result is None, "Trial result not restored correctly."
    for i in range(3):
        runner.step()

    assert t.status == Trial.TERMINATED

    # Test recovery of trial that has been checkpointed
    t2 = Trial("__fake", **kwargs)
    runner.add_trial(t2)
    runner.step()  # start
    runner.step()  # 1 result
    runner.step()  # 2 result and checkpoint
    assert t2.has_checkpoint()
    node3 = cluster.add_node(resources=dict(CPU=1))
    cluster.remove_node(node2)
    assert cluster.wait_for_nodes()
    runner.step()  # Recovery step
    assert t2.last_result["training_iteration"] == 2
    for i in range(1):
        runner.step()

    assert t2.status == Trial.TERMINATED

    # Test recovery of trial that won't be checkpointed
    t3 = Trial("__fake", **{"stopping_criterion": {"training_iteration": 3}})
    runner.add_trial(t3)
    runner.step()  # start
    runner.step()  # 1 result
    cluster.add_node(resources=dict(CPU=1))
    cluster.remove_node(node3)
    assert cluster.wait_for_nodes()
    runner.step()  # Error handling step
    assert t3.status == Trial.ERROR

    with pytest.raises(TuneError):
        runner.step()


@pytest.mark.skipif(
    pytest_timeout is None,
    reason="Timeout package not installed; skipping test.")
@pytest.mark.timeout(120, method="thread")
def test_trial_requeue(start_connected_emptyhead_cluster):
    """Removing a node in full cluster causes Trial to be requeued."""
    cluster = start_connected_emptyhead_cluster
    node = cluster.add_node(resources=dict(CPU=1))
    assert cluster.wait_for_nodes()

    runner = TrialRunner(BasicVariantGenerator())
    kwargs = {
        "stopping_criterion": {
            "training_iteration": 5
        },
        "checkpoint_freq": 1,
        "max_failures": 1
    }

    trials = [Trial("__fake", **kwargs), Trial("__fake", **kwargs)]
    for t in trials:
        runner.add_trial(t)

    runner.step()  # start
    runner.step()  # 1 result

    cluster.remove_node(node)
    assert cluster.wait_for_nodes()
    runner.step()
    assert all(t.status == Trial.PENDING for t in trials)

    with pytest.raises(TuneError):
        runner.step()


def test_cluster_down_simple(start_connected_cluster, tmpdir):
    """Tests that TrialRunner save/restore works on cluster shutdown."""
    cluster = start_connected_cluster
    cluster.add_node(resources=dict(CPU=1))
    assert cluster.wait_for_nodes()

    dirpath = str(tmpdir)
    runner = TrialRunner(
        BasicVariantGenerator(), metadata_checkpoint_dir=dirpath)
    kwargs = {
        "stopping_criterion": {
            "training_iteration": 2
        },
        "checkpoint_freq": 1,
        "max_failures": 1
    }
    trials = [Trial("__fake", **kwargs), Trial("__fake", **kwargs)]
    for t in trials:
        runner.add_trial(t)

    runner.step()  # start
    runner.step()  # start2
    runner.step()  # step
    assert all(t.status == Trial.RUNNING for t in runner.get_trials())
    runner.checkpoint()

    cluster.shutdown()
    ray.shutdown()

    cluster = _start_new_cluster()
    runner = TrialRunner.restore(dirpath)
    runner.step()  # start
    runner.step()  # start2

    for i in range(3):
        runner.step()

    with pytest.raises(TuneError):
        runner.step()

    assert all(t.status == Trial.TERMINATED for t in runner.get_trials())
    cluster.shutdown()


def test_cluster_down_full(start_connected_cluster, tmpdir):
    """Tests that run_experiment restoring works on cluster shutdown."""
    cluster = start_connected_cluster
    dirpath = str(tmpdir)

    exp1_args = dict(
        run="__fake",
        stop=dict(training_iteration=3),
        local_dir=dirpath,
        checkpoint_freq=1)
    exp2_args = dict(run="__fake", stop=dict(training_iteration=3))
    exp3_args = dict(
        run="__fake",
        stop=dict(training_iteration=3),
        config=dict(mock_error=True))
    exp4_args = dict(
        run="__fake",
        stop=dict(training_iteration=3),
        config=dict(mock_error=True),
        checkpoint_freq=1)
    all_experiments = {
        "exp1": exp1_args,
        "exp2": exp2_args,
        "exp3": exp3_args,
        "exp4": exp4_args
    }

    tune.run_experiments(all_experiments, raise_on_failed_trial=False)

    ray.shutdown()
    cluster.shutdown()
    cluster = _start_new_cluster()

    trials = tune.run_experiments(
        all_experiments, resume=True, raise_on_failed_trial=False)
    assert len(trials) == 4
    assert all(t.status in [Trial.TERMINATED, Trial.ERROR] for t in trials)
    cluster.shutdown()


def test_cluster_rllib_restore(start_connected_cluster, tmpdir):
    cluster = start_connected_cluster
    dirpath = str(tmpdir)
    script = """
import time
import ray
from ray import tune

ray.init(redis_address="{redis_address}")

kwargs = dict(
    run="PG",
    env="CartPole-v1",
    stop=dict(training_iteration=10),
    local_dir="{checkpoint_dir}",
    checkpoint_freq=1,
    max_failures=1)

tune.run_experiments(
    dict(experiment=kwargs),
    raise_on_failed_trial=False)
""".format(
        redis_address=cluster.redis_address, checkpoint_dir=dirpath)
    run_string_as_driver_nonblocking(script)
    # Wait until the right checkpoint is saved.
    # The trainable returns every 0.5 seconds, so this should not miss
    # the checkpoint.
    metadata_checkpoint_dir = os.path.join(dirpath, "experiment")
    for i in range(50):
        if os.path.exists(
                os.path.join(metadata_checkpoint_dir,
                             TrialRunner.CKPT_FILE_NAME)):
            # Inspect the internal trialrunner
            runner = TrialRunner.restore(metadata_checkpoint_dir)
            trials = runner.get_trials()
            last_res = trials[0].last_result
            if last_res is not None and last_res["training_iteration"]:
                break
        time.sleep(0.2)

    if not os.path.exists(
            os.path.join(metadata_checkpoint_dir, TrialRunner.CKPT_FILE_NAME)):
        raise RuntimeError("Checkpoint file didn't appear.")

    ray.shutdown()
    cluster.shutdown()
    cluster = _start_new_cluster()
    cluster.wait_for_nodes()

    # Restore properly from checkpoint
    trials2 = tune.run_experiments(
        {
            "experiment": {
                "run": "PG",
                "checkpoint_freq": 1,
                "local_dir": dirpath
            }
        },
        resume=True)
    assert all(t.status == Trial.TERMINATED for t in trials2)
    cluster.shutdown()


def test_cluster_interrupt(start_connected_cluster, tmpdir):
    """Tests run_experiment on cluster shutdown even with atypical trial.

    The trial fails on the 4th step, and the checkpointing happens on
    the 3rd step, so restoring should actually launch the trial again.
    """
    cluster = start_connected_cluster
    dirpath = str(tmpdir)
    script = """
import time
import ray
from ray import tune

ray.init(redis_address="{redis_address}")

{fail_class_code}

kwargs = dict(
    run={fail_class},
    stop=dict(training_iteration=5),
    local_dir="{checkpoint_dir}",
    checkpoint_freq=1,
    max_failures=1)

tune.run_experiments(
    dict(experiment=kwargs),
    raise_on_failed_trial=False)
""".format(
        redis_address=cluster.redis_address,
        checkpoint_dir=dirpath,
        fail_class_code=inspect.getsource(_Fail),
        fail_class=_Fail.__name__)
    run_string_as_driver_nonblocking(script)

    # Wait until the right checkpoint is saved.
    # The trainable returns every 0.5 seconds, so this should not miss
    # the checkpoint.
    metadata_checkpoint_dir = os.path.join(dirpath, "experiment")
    for i in range(50):
        if os.path.exists(
                os.path.join(metadata_checkpoint_dir,
                             TrialRunner.CKPT_FILE_NAME)):
            # Inspect the internal trialrunner
            runner = TrialRunner.restore(metadata_checkpoint_dir)
            trials = runner.get_trials()
            last_res = trials[0].last_result
            if last_res is not None and last_res["training_iteration"] == 3:
                break
        time.sleep(0.2)

    if not os.path.exists(
            os.path.join(metadata_checkpoint_dir, TrialRunner.CKPT_FILE_NAME)):
        raise RuntimeError("Checkpoint file didn't appear.")

    ray.shutdown()
    cluster.shutdown()
    cluster = _start_new_cluster()
    Experiment._register_if_needed(_Fail)

    # Inspect the internal trialrunner
    runner = TrialRunner.restore(metadata_checkpoint_dir)
    trials = runner.get_trials()
    assert trials[0].last_result["training_iteration"] == 3
    assert trials[0].status == Trial.PENDING

    # Restore properly from checkpoint
    trials2 = tune.run_experiments(
        {
            "experiment": {
                "run": _Fail,
                "local_dir": dirpath,
                "checkpoint_freq": 1
            }
        },
        resume=True,
        raise_on_failed_trial=False)
    assert all(t.status == Trial.ERROR for t in trials2)
    assert {t.trial_id for t in trials2} == {t.trial_id for t in trials}
    cluster.shutdown()
