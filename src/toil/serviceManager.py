# Copyright (C) 2015-2021 Regents of the University of California
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import logging
import time
from queue import Empty, Queue
from threading import Event, Thread
from typing import Dict, Set, Optional

from toil.job import ServiceJobDescription
from toil.lib.throttle import LocalThrottle, throttle
from toil.jobStores.abstractJobStore import AbstractJobStore
from toil.toilState import ToilState

logger = logging.getLogger( __name__ )

class ServiceManager( object ):
    """
    Manages the scheduling of services.
    """
    def __init__(self, job_store: AbstractJobStore, toil_state: ToilState):
        logger.debug("Initializing service manager")
        self.__job_store = job_store

        self.__toil_state = toil_state

        # We call the jobs that have services they need "client" jobs.

        # These are all the client jobs that are waiting for their services to
        # start.
        self.__waiting_clients: Set[str] = set()

        # This is used to terminate the thread associated with the service
        # manager
        self.__terminate = Event()

        # This is the input queue of jobs that have services that need to be started
        self.__clients_in: Queue[str] = Queue()

        # This is the output queue of jobs that have services that
        # are already started
        self.__clients_out: Queue[str] = Queue()

        # This is the output queue of jobs that have services that are unable
        # to start
        self.__failed_clients_out: Queue[str] = Queue()

        # This is the queue of services for the batch system to start
        self.__services_out: Queue[str] = Queue()

        self.__service_manager_jobs = 0 # The number of jobs the service manager is scheduling

        # Set up the service-managing thread.
        self.__service_starter = Thread(target=self.__start_services, daemon=True)

    def services_are_starting(self, job_id: str) -> bool:
        """
        Return True if the services for the given job are currently being started, and False otherwise.
        """
        return job_id in self.__waiting_clients

    def get_job_count(self) -> int:
        """
        Get the total number of jobs we are working on (services and their parent non-service jobs).
        """

        return self.__service_manager_jobs

    def start(self) -> None:
        """
        Start the service scheduling thread.
        """
        self.__service_starter.start()

    def scheduleServices(self, job_desc: ServiceJobDescription) -> None:
        """
        Schedule the services of a job asynchronously.
        When the job's services are running the JobDescription for the job will
        be returned by toil.leader.ServiceManager.getJobDescriptionWhoseServicesAreRunning.

        :param toil.job.JobDescription jobDesc: description job with services to schedule.
        """

        logger.debug("Service manager queueing %s as client", job_desc)

        # Add job to set being processed by the service manager
        self.__waiting_clients.add(job_desc.jobStoreID)

        # Add number of jobs managed by ServiceManager
        self.__service_manager_jobs += len(job_desc.services) + 1 # The plus one accounts for the root job

        # Asynchronously schedule the services
        self.__clients_in.put(job_desc.jobStoreID)

    def getJobDescriptionWhoseServicesAreRunning(self, maxWait: float) -> Optional[ServiceJobDescription]:
        """
        :param float maxWait: Time in seconds to wait to get a JobDescription before returning
        :return: a JobDescription added to scheduleServices whose services are running, or None if
        no such job is available.
        :rtype: toil.job.JobDescription
        """
        try:
            client_id = self.__clients_out.get(timeout=maxWait)
            self.__waiting_clients.remove(client_id)
            assert self.__service_manager_jobs >= 0
            self.__service_manager_jobs -= 1
            return self.__toil_state.get_job(client_id)
        except Empty:
            return None

    def getJobDescriptionWhoseServicesFailedToStart(self, maxWait: float) -> Optional[ServiceJobDescription]:
        """
        :param float maxWait: Time in seconds to wait to get a JobDescription before returning
        :return: a JobDescription added to scheduleServices whose services failed to start, or None if
        no such job is available.
        :rtype: toil.job.JobDescription
        """
        try:
            client_id = self.__failed_clients_out.get(timeout=maxWait)
            self.__waiting_clients.remove(client_id)
            assert self.__service_manager_jobs >= 0
            self.__service_manager_jobs -= 1
            return self.__toil_state.get_job(client_id)
        except Empty:
            return None

    def getServiceJobToStart(self, maxWait: float) -> Optional[str]:
        """
        :param float maxWait: Time in seconds to wait to get a job before returning.
        :return: the ID of a service job that the leader can start, or None if no such job exists.
        """
        try:
            service_id = self.__services_out.get(timeout=maxWait)
            assert self.__service_manager_jobs >= 0
            self.__service_manager_jobs -= 1
            return service_id
        except Empty:
            return None

    def killServices(self, services: Dict[str, ServiceJobDescription], error: bool =False) -> None:
        """
        :param dict services: Maps service jobStoreIDs to the communication flags for the service
        """
        for service_id in services:
            service = services[service_id]
            if error:
                self.__job_store.deleteFile(service.errorJobStoreID)
            self.__job_store.deleteFile(service.terminateJobStoreID)

    def isActive(self, service: ServiceJobDescription) -> bool:
        """
        Returns true if the service job has not been told to terminate.

        :param toil.job.JobDescription service: Service to check on
        :rtype: boolean
        """
        return self.__job_store.fileExists(service.terminateJobStoreID)

    def isRunning(self, service: ServiceJobDescription) -> bool:
        """
        Returns true if the service job has started and is active

        :param toil.job.JobDescription service: Service to check on
        :rtype: boolean
        """
        return (not self.__job_store.fileExists(service.startJobStoreID)) and self.isActive(service)

    def check(self) -> None:
        """
        Check on the service manager thread.
        :raise RuntimeError: If the underlying thread has quit.
        """
        if not self.__service_starter.is_alive():
            raise RuntimeError("Service manager has quit")

    def shutdown(self) -> None:
        """
        Cleanly terminate worker threads starting and killing services. Will block
        until all services are started and blocked.
        """
        logger.debug('Waiting for service manager thread to finish ...')
        start_time = time.time()
        self.__terminate.set()
        self.__service_starter.join()
        # Kill any services still running to avoid deadlock
        for services in list(self.__toil_state.servicesIssued.values()):
            self.killServices(services, error=True)
        logger.debug('... finished shutting down the service manager. Took %s seconds', time.time() - start_time)

    def __start_services(self) -> None:
        """
        Thread used to schedule services.
        """

        # Keep the user informed, but not too informed, as services start up
        log_limiter = LocalThrottle(60)

        # These are all keyed by ID
        starting_services = set()
        remaining_services_by_client = {}
        service_to_client = {}
        clients_with_failed_services = set()
        while True:
            with throttle(1.0):
                if self.__terminate.is_set():
                    logger.debug('Received signal to quit starting services.')
                    break
                try:
                    client_id = self.__clients_in.get_nowait()
                    client = self.__toil_state.get_job(client_id)
                    host_id_batches = list(client.serviceHostIDsInBatches())
                    logger.debug("Service manager processing client %s with %d batches of services", client, len(host_id_batches))
                    if len(host_id_batches) > 1:
                        # Have to fall back to the old blocking behavior to
                        # ensure entire service "groups" are issued as a whole.
                        self.__start_batches_blocking(client_id)
                        continue
                    # Found a new job that needs to schedule its services.
                    for batch in host_id_batches:
                        # There should be just one batch so we can do it here.
                        remaining_services_by_client[client_id] = len(batch)
                        for service_id in batch:
                            # Load up the service object.
                            service_job_desc = self.__toil_state.get_job(service_id)
                            # Remember the parent job
                            service_to_client[service_id] = client_id
                            # We should now start to monitor this service to see if
                            # it has started yet.
                            starting_services.add(service_id)
                            # Send the service JobDescription off to be started
                            logger.debug('Service manager is starting service job: %s, start ID: %s', service_job_desc, service_job_desc.startJobStoreID)
                            self.__services_out.put(service_id)
                except Empty:
                    # No new jobs that need services scheduled.
                    pass

                pending_service_count = len(starting_services)
                if pending_service_count > 0 and log_limiter.throttle(False):
                    logger.debug('%d services are starting...', pending_service_count)

                for service_id in list(starting_services):
                    service_job_desc = self.__toil_state.get_job(service_id)
                    if not self.__job_store.fileExists(service_job_desc.startJobStoreID):
                        # Service has started (or failed)
                        logger.debug('Service %s has removed %s and is therefore started', service_job_desc, service_job_desc.startJobStoreID)
                        starting_services.remove(service_id)
                        client_id = service_to_client[service_id]
                        remaining_services_by_client[client_id] -= 1
                        assert remaining_services_by_client[client_id] >= 0
                        del service_to_client[service_id]
                        if not self.__job_store.fileExists(service_job_desc.errorJobStoreID):
                            logger.error('Service %s has immediately failed before it could be used', service_job_desc)
                            # It probably hasn't fileld in the promise that the job that uses the service needs.
                            clients_with_failed_services.add(client_id)

                # Find if any clients have had *all* their services started.
                ready_clients = set()
                for client_id, remainingServices in remaining_services_by_client.items():
                    if remainingServices == 0:
                        if client_id in clients_with_failed_services:
                            logger.error('Job %s has had all its services try to start, but at least one failed', self.__toil_state.get_job(client_id))
                            self.__failed_clients_out.put(client_id)
                        else:
                            logger.debug('Job %s has all its services started', self.__toil_state.get_job(client_id))
                            self.__clients_out.put(client_id)
                        ready_clients.add(client_id)
                for client_id in ready_clients:
                    del remaining_services_by_client[client_id]

    def __start_batches_blocking(self, client_id: str) -> None:
        """
        Wait until all the services for the given job are started, starting
        them in batches that are all issued together.
        """

        # Keep the user informed, but not too informed, as services start up
        log_limiter = LocalThrottle(60)

        # Start the service jobs in batches, waiting for each batch
        # to become established before starting the next batch
        for service_job_list in self.__toil_state.get_job(client_id).serviceHostIDsInBatches():
            # When we get the job descriptions we store them here to go over them again.
            wait_on = []
            for service_id in service_job_list:
                # Find the service object.
                service_job_desc = self.__toil_state.get_job(service_id)
                logger.debug("Service manager is starting service job: %s, start ID: %s", service_job_desc, service_job_desc.startJobStoreID)
                assert self.__job_store.fileExists(service_job_desc.startJobStoreID)
                # At this point the terminateJobStoreID and errorJobStoreID could have been deleted!
                self.__services_out.put(service_id)
                # Save for the waiting loop
                wait_on.append(service_job_desc)

            # Wait until all the services of the batch are running
            for service_id in service_job_list:
                # Find the service object.
                service_job_desc = self.__toil_state.get_job(service_id)
                while self.__job_store.fileExists(service_job_desc.startJobStoreID):
                    # Sleep to avoid thrashing
                    time.sleep(1.0)

                    if log_limiter.throttle(False):
                        logger.info('Service %s is starting...', service_job_desc)

                    # Check if the thread should quit
                    if self.__terminate.is_set():
                        return

                # We don't bail out early here.

                # We need to try and fail to start *all* the services, so they
                # *all* come back to the leaser as expected, or the leader will get
                # stuck waiting to hear about a later dependent service failing. So
                # we have to *try* to start all the services, even if the services
                # they depend on failed. They should already have been killed,
                # though, so they should stop immediately when we run them. TODO:
                # this is a bad design!


        # Add the JobDescription to the output queue of jobs whose services have been started
        self.__clients_out.put(client_id)
