import os
import sys
import json
import time
import pickle
import numpy as np
from typing import List
from copy import deepcopy
from loguru import logger
from distributed import Client
from mo_hyperband.utils import Trial
from mo_hyperband.utils import SHBracketManager
from mo_hyperband.utils import multi_obj_util
from sklearn.preprocessing import normalize

_logger_props = {
    "format": "{time:YYYY-MM-DD HH:mm:ss.SSS} {level} {message}",
    "enqueue": True,
    "rotation": "500 MB",
    "level": "INFO",
}


class MOHB:
    def __init__(self, cs=None, f=None, objectives=None, min_budget=None, mo_strategy=None,
                 max_budget=None, eta=3, min_clip=None, max_clip=None, n_workers=None, client=None, **kwargs):

        self.iteration_counter = -1
        self.cs = cs
        self.f = f

        # Hyperband related variables
        self.min_budget = min_budget
        self.max_budget = max_budget
        assert self.max_budget > self.min_budget, "only (Max Budget > Min Budget) supported!"
        self.eta = eta
        self.min_clip = min_clip
        self.max_clip = max_clip
        self.objectives = objectives
        self.mo_strategy = mo_strategy

        if self.mo_strategy['algorithm'] in multi_obj_util.scalarization_strategy:
            n_weights = self.mo_strategy.get('num_weights', 100)
            self.weights = [multi_obj_util.uniform_from_unit_simplex(len(self.objectives)) for _ in range(n_weights)]
            logger.debug(f'weights:{self.weights}')

        # Precomputing budget spacing and number of configurations for HB iterations
        self.max_SH_iter = None
        self.budgets = None
        if self.min_budget is not None and \
                self.max_budget is not None and \
                self.eta is not None:
            self.max_SH_iter = -int(np.log(self.min_budget / self.max_budget) / np.log(self.eta)) + 1
            self.budgets = self.max_budget * np.power(self.eta,
                                                      -np.linspace(start=self.max_SH_iter - 1,
                                                                   stop=0, num=self.max_SH_iter))
            import numbers
            if isinstance(min_budget, numbers.Integral):
                dtype = np.int64
            else:
                dtype = np.float64
            self.budgets = [budget.astype(dtype) for budget in self.budgets]

        # Miscellaneous
        self.output_path = kwargs['output_path'] if 'output_path' in kwargs else './'
        os.makedirs(self.output_path, exist_ok=True)
        self.logger = logger
        log_suffix = time.strftime("%x %X %Z")
        log_suffix = log_suffix.replace("/", '-').replace(":", '-').replace(" ", '_')
        self.logger.add(
            "{}/mohb_{}.log".format(self.output_path, log_suffix),
            **_logger_props
        )
        self.log_filename = "{}/mohb_{}.log".format(self.output_path, log_suffix)

        self.trials = []
        self.pareto_trials = []
        self.history = []
        self.active_brackets = []  # list of SHBracketManager objects
        self.runtime = []
        self.history = []
        self.start = None
        self.cumulated_costs = 0

        # Dask variables
        if n_workers is None and client is None:
            raise ValueError("Need to specify either 'n_workers'(>0) or 'client' (a Dask client)!")
        if client is not None and isinstance(client, Client):
            self.client = client
            self.n_workers = len(client.ncores())
        else:
            self.n_workers = n_workers
            if self.n_workers > 1:
                self.client = Client(
                    n_workers=self.n_workers, processes=True, threads_per_worker=1, scheduler_port=0
                )  # port 0 makes Dask select a random free port
            else:
                self.client = None
        self.futures = []
        self.shared_data = None

        # Misc.
        self.available_gpus = None
        self.gpu_usage = None
        self.single_node_with_gpus = None

    def sort_indices(self, cost, n_configs):
        if self.mo_strategy["algorithm"] == "EPSNET":
            ranked_top = multi_obj_util.get_eps_net_ranking(cost, n_configs)
        elif self.mo_strategy["algorithm"] == "NSGA-II":
            ranked_top = multi_obj_util.get_nsga_ii_ranking(cost, n_configs)
        elif self.mo_strategy["algorithm"] in multi_obj_util.scalarization_strategy:
            ranked_top = multi_obj_util.get_scalarization_ranking(cost,
                                                                  n_configs,
                                                                  self.mo_strategy,
                                                                  self.weights)
        else:
            raise ValueError("Specified algorithm is unknown. \
                       Valid algorithms are 'random_weights', 'parego', golovin, NSGA-II and EPSNET.")
        return ranked_top

    def reset(self):
        self.trials = []
        self.population = None
        self.fitness = None
        self.traj = []
        self.runtime = []
        self.history = []
        self.logger.info("\n\nRESET at {}\n\n".format(time.strftime("%x %X %Z")))

    def get_next_iteration(self, iteration):
        '''Computes the Successive Halving spacing

        Given the iteration index, computes the budget spacing to be used and
        the number of configurations to be used for the SH iterations.

        Parameters
        ----------
        iteration : int
            Iteration index
        clip : int, {1, 2, 3, ..., None}
            If not None, clips the minimum number of configurations to 'clip'

        Returns
        -------
        ns : array
        budgets : array
        '''
        # number of 'SH runs'
        s = self.max_SH_iter - 1 - (iteration % self.max_SH_iter)
        # budget spacing for this iteration
        budgets = self.budgets[(-s - 1):]
        # number of configurations in that bracket
        n0 = int(np.floor((self.max_SH_iter) / (s + 1)) * self.eta ** s)
        ns = [max(int(n0 * (self.eta ** (-i))), 1) for i in range(s + 1)]
        if self.min_clip is not None and self.max_clip is not None:
            ns = np.clip(ns, a_min=self.min_clip, a_max=self.max_clip)
        elif self.min_clip is not None:
            ns = np.clip(ns, a_min=self.min_clip, a_max=np.max(ns))

        return ns, budgets

    def __getstate__(self):
        """ Allows the object to picklable while having Dask client as a class attribute.
        """
        d = dict(self.__dict__)
        d["client"] = None  # hack to allow Dask client to be a class attribute
        d["logger"] = None  # hack to allow logger object to be a class attribute
        return d

    def __del__(self):
        """ Ensures a clean kill of the Dask client and frees up a port.
        """
        if hasattr(self, "client") and isinstance(self, Client):
            self.client.close()

    def f_objective(self, config, budget=None, **kwargs):
        if self.f is None:
            raise NotImplementedError("An objective function needs to be passed.")
        if budget is not None:  # to be used when called by multi-fidelity based optimizers
            res = self.f(config, budget=budget, **kwargs)
        else:
            res = self.f(config, **kwargs)
        assert "function_value" in res
        assert "cost" in res
        return res

    def _f_objective(self, job_info):
        """ Wrapper to call MO's objective function.
        """
        # check if job_info appended during job submission self.submit_job() includes "gpu_devices"
        if "gpu_devices" in job_info and self.single_node_with_gpus:
            # should set the environment variable for the spawned worker process
            # reprioritising a CUDA device order specific to this worker process
            os.environ.update({"CUDA_VISIBLE_DEVICES": job_info["gpu_devices"]})

        config, budget = job_info['config'], job_info['budget']
        bracket_id = job_info['bracket_id']
        kwargs = job_info["kwargs"]
        res = self.f_objective(config, budget, **kwargs)
        info = res["info"] if "info" in res else dict()
        run_info = {
            'fitness': res["function_value"],
            'cost': res["cost"],
            'config': config,
            'budget': budget,
            'trial': job_info['trial'],
            'bracket_id': bracket_id,
            'info': info,
            'meta_data': res
        }

        if "gpu_devices" in job_info:
            # important for GPU usage tracking if single_node_with_gpus=True
            device_id = int(job_info["gpu_devices"].strip().split(",")[0])
            run_info.update({"device_id": device_id})
        return run_info

    def _create_cuda_visible_devices(self, available_gpus: List[int], start_id: int) -> str:
        """ Generates a string to set the CUDA_VISIBLE_DEVICES environment variable.

        Given a list of available GPU device IDs and a preferred ID (start_id), the environment
        variable is created by putting the start_id device first, followed by the remaining devices
        arranged randomly. The worker that uses this string to set the environment variable uses
        the start_id GPU device primarily now.
        """
        assert start_id in available_gpus
        available_gpus = deepcopy(available_gpus)
        available_gpus.remove(start_id)
        np.random.shuffle(available_gpus)
        final_variable = [str(start_id)] + [str(_id) for _id in available_gpus]
        final_variable = ",".join(final_variable)
        return final_variable

    def distribute_gpus(self):
        """ Function to create a GPU usage tracker dict.

        The idea is to extract the exact GPU device IDs available. During job submission, each
        submitted job is given a preference of a GPU device ID based on the GPU device with the
        least number of active running jobs. On retrieval of the result, this gpu usage dict is
        updated for the device ID that the finished job was mapped to.
        """
        try:
            available_gpus = os.environ["CUDA_VISIBLE_DEVICES"]
            available_gpus = available_gpus.strip().split(",")
            self.available_gpus = [int(_id) for _id in available_gpus]
        except KeyError as e:
            print("Unable to find valid GPU devices. "
                  "Environment variable {} not visible!".format(str(e)))
            self.available_gpus = []
        self.gpu_usage = dict()
        for _id in self.available_gpus:
            self.gpu_usage[_id] = 0

    def clean_inactive_brackets(self):
        """ Removes brackets from the active list if it is done as communicated by Bracket Manager
        """
        if len(self.active_brackets) == 0:
            return
        self.active_brackets = [
            bracket for bracket in self.active_brackets if ~bracket.is_bracket_done()
        ]
        return

    def _update_trackers(self, runtime, history):
        self.runtime.append(runtime)
        self.history.append(history)

    def _update_pareto(self):
        fitness = [trial.get_fitness() for trial in self.trials]
        logger.debug(f'fitness to be checked for pareto:{fitness}')
        index_list = np.array(range(len(fitness)))
        is_pareto, _ = multi_obj_util.pareto_index(np.array(fitness), index_list)
        return list(np.array(self.trials)[is_pareto])

    def _get_pop_sizes(self):
        """Determines maximum pop size for each budget
        """
        self._max_pop_size = {}
        for i in range(self.max_SH_iter):
            n, r = self.get_next_iteration(i)
            for j, r_j in enumerate(r):
                self._max_pop_size[r_j] = max(
                    n[j], self._max_pop_size[r_j]
                ) if r_j in self._max_pop_size.keys() else n[j]

    def _start_new_bracket(self):
        """ Starts a new bracket based on Hyperband
        """
        # start new bracket
        self.iteration_counter += 1  # iteration counter gives the bracket count or bracket ID
        n_configs, budgets = self.get_next_iteration(self.iteration_counter)
        logger.debug(f'n_configs:{n_configs}, budgets:{budgets}')
        bracket = SHBracketManager(
            n_configs=n_configs, budgets=budgets, bracket_id=self.iteration_counter
        )
        self.active_brackets.append(bracket)
        return bracket

    def _get_worker_count(self):
        if isinstance(self.client, Client):
            return len(self.client.ncores())
        else:
            return 1

    def is_worker_available(self, verbose=False):
        """ Checks if at least one worker is available to run a job
        """
        if self.n_workers == 1 or self.client is None or not isinstance(self.client, Client):
            # in the synchronous case, one worker is always available
            return True
        # checks the absolute number of workers mapped to the client scheduler
        # client.ncores() should return a dict with the keys as unique addresses to these workers
        # treating the number of available workers in this manner
        workers = self._get_worker_count()  # len(self.client.ncores())
        if len(self.futures) >= workers:
            # pause/wait if active worker count greater allocated workers
            return False
        return True

    def _acquire_config(self, bracket, budget):
        """ Generates/chooses a configuration based on the budget and iteration number
        """
        assert budget in bracket.budgets

        if budget not in bracket.trials:
            # init population randomly for base rung
            if budget == bracket.budgets[0]:
                logger.debug(f'Randomly initializing population for budget:{budget} and bracket:{bracket.bracket_id}')
                pop_size = bracket.n_configs[0]
                population = self.cs.sample_configuration(size=pop_size)
                candidate_trials = [Trial(individual.get_dictionary()) for individual in population]
                trial_config = [trial.config for trial in candidate_trials]
                logger.debug(f'Trials generated:{trial_config}')
            # Promote candidates from lower budget for next rung
            else:
                # identify lower budget/fidelity to transfer information from
                lower_budget, n_configs = bracket.get_lower_budget_promotions(budget)
                logger.debug(f'Promoting configuration from budget:{lower_budget} to '
                             f'budget:{budget} and bracket:{bracket.bracket_id}')
                candidate_trials = bracket.trials[lower_budget]

                # get fitness of candidates
                fitness = [trial.get_fitness() for trial in candidate_trials]
                logger.debug(f'trials fitness:{fitness}')

                normalize_fitness = normalize(fitness, axis=0, norm='max')
                logger.debug(f'normalize fitness:{normalize_fitness}')

                # sort candidates according to fitness
                sorted_index = self.sort_indices(normalize_fitness, n_configs)
                logger.debug(f'sorted index:{sorted_index}')
                trials = np.array(candidate_trials)[sorted_index]
                trial_fitness = [trial.get_fitness() for trial in trials]
                logger.debug(f'trial promoted from budget:{lower_budget} to budget:{budget}:{trial_fitness}')

                # Create new instances to not lose information
                candidate_trials = [Trial(trial.config) for trial in trials]

            # populate the trials for the budget
            bracket.trials[budget] = candidate_trials

        logger.debug(f'budget:{budget}')
        pending_trials = bracket.get_pending_trials(budget)
        logger.debug(f'pending trials:{len(pending_trials)}')
        return pending_trials[0]

    def _get_next_job(self):
        """ Loads a configuration and budget to be evaluated next by a free worker
        """
        bracket = None
        if len(self.active_brackets) == 0 or \
                np.all([bracket.is_bracket_done() for bracket in self.active_brackets]):
            # start new bracket when no pending jobs from existing brackets or empty bracket list
            bracket = self._start_new_bracket()
        else:
            for _bracket in self.active_brackets:
                # check if _bracket is not waiting for previous rung results of same bracket
                # _bracket is not waiting on the last rung results
                # these 2 checks allow HB to have a "synchronous" Successive Halving
                if not _bracket.previous_rung_waits() and _bracket.is_pending():
                    # bracket eligible for job scheduling
                    bracket = _bracket
                    break
            if bracket is None:
                # start new bracket when existing list has all waiting brackets
                bracket = self._start_new_bracket()
        # budget that the SH bracket allots
        budget = bracket.get_next_job_budget()
        trial = self._acquire_config(bracket, budget)
        # notifies the Bracket Manager that a single config is to run for the budget chosen
        job_info = {
            "config": trial.config,
            "budget": budget,
            "trial": trial,
            "bracket_id": bracket.bracket_id
        }
        return job_info

    def _get_gpu_id_with_low_load(self):
        candidates = []
        for k, v in self.gpu_usage.items():
            if v == min(self.gpu_usage.values()):
                candidates.append(k)
        device_id = np.random.choice(candidates)
        # creating string for setting environment variable CUDA_VISIBLE_DEVICES
        gpu_ids = self._create_cuda_visible_devices(
            self.available_gpus, device_id
        )
        # updating GPU usage
        self.gpu_usage[device_id] += 1
        self.logger.debug("GPU device selected: {}".format(device_id))
        self.logger.debug("GPU device usage: {}".format(self.gpu_usage))
        return gpu_ids

    def submit_job(self, job_info, **kwargs):
        """ Asks a free worker to run the objective function on config and budget
        """
        job_info["kwargs"] = self.shared_data if self.shared_data is not None else kwargs
        # submit to to Dask client
        if self.n_workers > 1 or isinstance(self.client, Client):
            if self.single_node_with_gpus:
                # managing GPU allocation for the job to be submitted
                job_info.update({"gpu_devices": self._get_gpu_id_with_low_load()})
            self.futures.append(
                self.client.submit(self._f_objective, job_info)
            )
        else:
            # skipping scheduling to Dask worker to avoid added overheads in the synchronous case
            self.futures.append(self._f_objective(job_info))

        # pass information of job submission to Bracket Manager
        for bracket in self.active_brackets:
            if bracket.bracket_id == job_info['bracket_id']:
                # registering is IMPORTANT for Bracket Manager to perform SH
                trial = job_info['trial']
                logger.debug(f'trial:{trial.config},fitness:{trial._fitness},'
                             f' status:{trial._status}, meta_info:{trial._metainfo}')
                bracket.register_job(job_info['budget'], job_info['trial'])
                break

    def _fetch_results_from_workers(self):
        """ Iterate over futures and collect results from finished workers
        """
        if self.n_workers > 1 or isinstance(self.client, Client):
            done_list = [(i, future) for i, future in enumerate(self.futures) if future.done()]
        else:
            # Dask not invoked in the synchronous case
            done_list = [(i, future) for i, future in enumerate(self.futures)]
        if len(done_list) > 0:
            self.logger.debug(
                "Collecting {} of the {} job(s) active.".format(len(done_list), len(self.futures))
            )
        for _, future in done_list:
            if self.n_workers > 1 or isinstance(self.client, Client):
                run_info = future.result()
                if "device_id" in run_info:
                    # updating GPU usage
                    self.gpu_usage[run_info["device_id"]] -= 1
                    self.logger.debug("GPU device released: {}".format(run_info["device_id"]))
                future.release()
            else:
                # Dask not invoked in the synchronous case
                run_info = future
            # update bracket information
            fitness, cost = run_info["fitness"], run_info["cost"]
            assert all(item in fitness.keys() for item in self.objectives)

            info = run_info["info"] if "info" in run_info else dict()
            budget = run_info["budget"]
            config = run_info["config"]
            bracket_id = run_info["bracket_id"]
            self.cumulated_costs += cost

            logger.debug(f'run info:{run_info}')
            for bracket in self.active_brackets:
                if bracket.bracket_id == bracket_id:
                    # bracket job complete
                    logger.debug(f'fitness for config:{list(fitness.values())}')
                    bracket.complete_job(budget, run_info)  # IMPORTANT to perform synchronous SH

            # book-keeping
            self.trials.append(run_info['trial'])
            self._update_trackers(runtime=cost, history=(
                config, dict(fitness), float(cost), float(budget), info))
        # remove processed future
        self.futures = np.delete(self.futures, [i for i, _ in done_list]).tolist()

    def _is_run_budget_exhausted(self, fevals=None, brackets=None, total_cost=None, total_wallclock_cost=None):
        """ Checks if the DEHB run should be terminated or continued
        """
        delimiters = [fevals, brackets, total_cost, total_wallclock_cost]
        delim_sum = sum(x is not None for x in delimiters)
        if delim_sum == 0:
            raise ValueError(
                "Need one of 'fevals', 'brackets', 'total_cost' or 'total_wallclock_cost' as budget for DEHB to run."
            )

        if fevals is not None:
            if len(self.traj) >= fevals:
                return True

        if brackets is not None:
            if self.iteration_counter >= brackets:
                for bracket in self.active_brackets:
                    # waits for all brackets < iteration_counter to finish by collecting results
                    if bracket.bracket_id < self.iteration_counter and \
                            not bracket.is_bracket_done():
                        return False
                return True

        if total_cost is not None:
            if self.cumulated_costs >= total_cost:
                return True

        if total_wallclock_cost is not None:
            if time.time() - self.start >= total_wallclock_cost:
                return True
            if len(self.runtime) > 0 and self.runtime[-1] - self.start >= total_wallclock_cost:
                return True

        return False

    def _save_incumbent(self, name=None):
        self.pareto_trials = self._update_pareto()

        if name is None:
            name = time.strftime("%x %X %Z", time.localtime(self.start))
            name = name.replace("/", '-').replace(":", '-').replace(" ", '_')
        try:
            with open(os.path.join(self.output_path, "incumbents_{}.json".format(name)), 'w') as f:
                json.dump([ob.__dict__ for ob in self.pareto_trials], f)
        except Exception as e:
            self.logger.warning("Pareto not saved: {}".format(repr(e)))

    def _save_history(self, name=None):
        if name is None:
            name = time.strftime("%x %X %Z", time.localtime(self.start))
            name = name.replace("/", '-').replace(":", '-').replace(" ", '_')
        try:
            with open(os.path.join(self.output_path, "history_{}.pkl".format(name)), 'wb') as f:
                pickle.dump(self.history, f)
        except Exception as e:
            self.logger.warning("History not saved: {}".format(repr(e)))

    def _verbosity_debug(self):
        for bracket in self.active_brackets:
            self.logger.debug("Bracket ID {}:\n{}".format(
                bracket.bracket_id,
                str(bracket)
            ))

    def _verbosity_runtime(self, fevals, brackets, total_cost, total_wallclock_cost):

        if fevals is not None:
            self.logger.info(f"{len(self.traj)}/{fevals} function evaluation(s) done")

        if brackets is not None:
            _suffix = "bracket(s) started; # active brackets: {}".format(len(self.active_brackets))
            self.logger.info(f"{self.iteration_counter + 1}/{brackets} {_suffix}")

        elif total_cost is not None:
            elapsed = np.format_float_positional(self.cumulated_costs, precision=2)
            self.logger.info(f"{elapsed}/{total_cost} seconds elapsed")

        else:
            elapsed = np.format_float_positional(time.time() - self.start, precision=2)
            self.logger.info(f"{elapsed}/{total_wallclock_cost} seconds (wallclock time) elapsed")

    @logger.catch
    def run(self, fevals=None, brackets=None, total_cost=None, total_wallclock_cost=None,  single_node_with_gpus=False,
            verbose=False, debug=False, save_intermediate=True, save_history=True, **kwargs):
        """ Main interface to run optimization by DEHB

        This function waits on workers and if a worker is free, asks for a configuration and a
        budget to evaluate on and submits it to the worker. In each loop, it checks if a job
        is complete, fetches the results, carries the necessary processing of it asynchronously
        to the worker computations.

        The duration of the DEHB run can be controlled by specifying one of 4 parameters.
        MODIFIED: If a defined limit is reached, the run will be stopped.
        1) Number of function evaluations (fevals)
        2) Number of Successive Halving brackets run under Hyperband (brackets)
        3) Total computational cost (in seconds) aggregated by all function evaluations (total_wallclock_cost)
        4) Total computational cost (in seconds) returned by the objective function. Might be simulated costs. (total_cost)
        """
        # checks if a Dask client exists
        if len(kwargs) > 0 and self.n_workers > 1 and isinstance(self.client, Client):
            # broadcasts all additional data passed as **kwargs to all client workers
            # this reduces overload in the client-worker communication by not having to
            # serialize the redundant data used by all workers for every job
            self.shared_data = self.client.scatter(kwargs, broadcast=True)

        # allows each worker to be mapped to a different GPU when running on a single node
        # where all available GPUs are accessible
        self.single_node_with_gpus = single_node_with_gpus
        if self.single_node_with_gpus:
            self.distribute_gpus()

        self.start = time.time()
        if verbose:
            print("\nLogging at {} for optimization starting at {}\n".format(
                os.path.join(os.getcwd(), self.log_filename),
                time.strftime("%x %X %Z", time.localtime(self.start))
            ))
        if debug:
            logger.configure(handlers=[{"sink": sys.stdout}])
        while True:
            if self._is_run_budget_exhausted(fevals, brackets, total_cost, total_wallclock_cost):
                break
            if self.is_worker_available():
                job_info = self._get_next_job()
                if brackets is not None and job_info['bracket_id'] >= brackets:
                    # ignore submission and only collect results
                    # when brackets are chosen as run budget, an extra bracket is created
                    # since iteration_counter is incremented in _get_next_job() and then checked
                    # in _is_run_budget_exhausted(), therefore, need to skip suggestions
                    # coming from the extra allocated bracket
                    # _is_run_budget_exhausted() will not return True until all the lower brackets
                    # have finished computation and returned its results
                    pass
                else:
                    if self.n_workers > 1 or isinstance(self.client, Client):
                        self.logger.debug("{}/{} worker(s) available.".format(
                            self._get_worker_count() - len(self.futures), self._get_worker_count()
                        ))
                    # submits job_info to a worker for execution
                    self.submit_job(job_info, **kwargs)
                    if verbose:
                        budget = job_info['budget']
                        self._verbosity_runtime(fevals, brackets, total_cost, total_wallclock_cost)
                        self.logger.info(
                            "Evaluating a configuration with budget {} under "
                            "bracket ID {}".format(budget, job_info['bracket_id'])
                        )
                        self.pareto_trials = self._update_pareto()
                        pareto_fitness = [trial.get_fitness() for trial in self.pareto_trials]
                        self.logger.info(
                            "Best score seen/Incumbent score: {}".format(pareto_fitness)
                        )
                    self._verbosity_debug()
            self._fetch_results_from_workers()
            if save_intermediate and self.trials is not None:
                self._save_incumbent()
            if save_history and self.history is not None:
                self._save_history()
            self.clean_inactive_brackets()
        # end of while

        if verbose and len(self.futures) > 0:
            self.logger.info(
                "MOHB optimisation over! Waiting to collect results from workers running..."
            )
        while len(self.futures) > 0:
            self._fetch_results_from_workers()
            if save_intermediate and self.trials is not None:
                self._save_incumbent()
            if save_history and self.history is not None:
                self._save_history()
            time.sleep(0.05)  # waiting 50ms

        self._save_incumbent()
        self._save_history()

        if verbose:
            time_taken = time.time() - self.start
            self.logger.info("End of optimisation! Total duration: {}; Total fevals: {}\n".format(
                time_taken, len(self.traj)
            ))
            self.logger.info("pareto score: {}".format([trial.get_fitness() for trial in self.pareto_trials]))
            configs = [trial.config for trial in self.pareto_trials]
            self.logger.info("Incumbent config: ")
            for config in configs:
                for k, v in config.items():
                    self.logger.info("{}: {}".format(k, v))

        return np.array(self.runtime), np.array(self.history, dtype=object)
