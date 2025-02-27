#!/usr/bin/env python
#
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
import logging
import os
import pkgutil
import re
import subprocess  # nosec
import uuid
from enum import Enum
from shutil import which
from typing import Callable, Dict, List, Optional, Tuple, Type, TypeVar, Union
from urllib.parse import urlparse
from uuid import UUID

import semver
from memoization import cached
from onefuzztypes import (
    enums,
    events,
    models,
    primitives,
    requests,
    responses,
    webhooks,
)
from onefuzztypes.enums import TaskType
from pydantic import BaseModel
from requests import Response
from six.moves import input  # workaround for static analysis

from .__version__ import __version__
from .azcopy import azcopy_sync
from .backend import Backend, BackendConfig, ContainerWrapper

UUID_EXPANSION = TypeVar("UUID_EXPANSION", UUID, str)

DEFAULT = BackendConfig(endpoint="")

# This was generated randomly and should be preserved moving forwards
ONEFUZZ_GUID_NAMESPACE = uuid.UUID("27f25e3f-6544-4b69-b309-9b096c5a9cbc")

ONE_HOUR_IN_SECONDS = 3600

REPRO_SSH_FORWARD = "1337:127.0.0.1:1337"

UUID_RE = r"^[a-f0-9]{8}-?[a-f0-9]{4}-?[a-f0-9]{4}-?[a-f0-9]{4}-?[a-f0-9]{12}\Z"

# Environment variable optionally used for setting an application client secret.
CLIENT_SECRET_ENV_VAR = "ONEFUZZ_CLIENT_SECRET"  # nosec


class PreviewFeature(Enum):
    job_templates = "job_templates"


def is_uuid(value: str) -> bool:
    return bool(re.match(UUID_RE, value))


A = TypeVar("A", bound=BaseModel)


def _wsl_path(path: str) -> str:
    if which("wslpath"):
        # security note: path should always be a temporary path constructed by
        # this library
        return (
            subprocess.check_output(["wslpath", "-w", path]).decode().strip()  # nosec
        )
    return path


def user_confirmation(message: str) -> bool:
    answer: Optional[str] = None
    while answer not in ["y", "n"]:
        answer = input(message).strip()

    if answer == "n":
        return False
    return True


class Endpoint:
    endpoint: str

    def __init__(self, onefuzz: "Onefuzz"):
        self.onefuzz = onefuzz
        self.logger = onefuzz.logger

    def _req_base(
        self,
        method: str,
        *,
        data: Optional[BaseModel] = None,
        as_params: bool = False,
        alternate_endpoint: Optional[str] = None,
    ) -> Response:
        endpoint = self.endpoint if alternate_endpoint is None else alternate_endpoint

        if as_params:
            response = self.onefuzz._backend.request(method, endpoint, params=data)
        else:
            response = self.onefuzz._backend.request(method, endpoint, json_data=data)

        return response

    def _req_model(
        self,
        method: str,
        model: Type[A],
        *,
        data: Optional[BaseModel] = None,
        as_params: bool = False,
        alternate_endpoint: Optional[str] = None,
    ) -> A:
        response = self._req_base(
            method,
            data=data,
            as_params=as_params,
            alternate_endpoint=alternate_endpoint,
        ).json()

        return model.parse_obj(response)

    def _req_model_list(
        self,
        method: str,
        model: Type[A],
        *,
        data: Optional[BaseModel] = None,
        as_params: bool = False,
        alternate_endpoint: Optional[str] = None,
    ) -> List[A]:
        endpoint = self.endpoint if alternate_endpoint is None else alternate_endpoint

        if as_params:
            response = self.onefuzz._backend.request(
                method, endpoint, params=data
            ).json()
        else:
            response = self.onefuzz._backend.request(
                method, endpoint, json_data=data
            ).json()

        return [model.parse_obj(x) for x in response]

    def _disambiguate(
        self,
        name: str,
        value: str,
        check: Callable[[str], bool],
        func: Callable[[], List[str]],
    ) -> str:
        if check(value):
            return value

        self.logger.debug("expanding %s: %s", name, value)

        values = [x for x in func() if x.startswith(value)]
        if len(values) == 1:
            return values[0]

        if len(values) > 1:
            if value in values:
                return value
            raise Exception(
                "%s expands to multiple values - %s: %s"
                % (name, value, ",".join(values))
            )

        raise Exception("Unable to find %s based on prefix: %s" % (name, value))

    def _disambiguate_uuid(
        self,
        name: str,
        value: UUID_EXPANSION,
        func: Callable[[], List[str]],
    ) -> UUID:
        if isinstance(value, UUID):
            return value
        return UUID(self._disambiguate(name, value, is_uuid, func))


class Files(Endpoint):
    """Interact with files within a container"""

    endpoint = "files"

    @cached(ttl=ONE_HOUR_IN_SECONDS)
    def _get_client(self, container: primitives.Container) -> ContainerWrapper:
        sas = self.onefuzz.containers.get(container).sas_url
        return ContainerWrapper(sas)

    def list(
        self, container: primitives.Container, prefix: Optional[str] = None
    ) -> models.Files:
        """Get a list of files in a container"""
        self.logger.debug("listing files in container: %s", container)
        client = self._get_client(container)
        return models.Files(files=client.list_blobs(name_starts_with=prefix))

    def delete(self, container: primitives.Container, filename: str) -> None:
        """delete a file from a container"""
        self.logger.debug("deleting in container: %s:%s", container, filename)
        client = self._get_client(container)
        client.delete_blob(filename)

    def get(self, container: primitives.Container, filename: str) -> bytes:
        """get a file from a container"""
        self.logger.debug("getting file from container: %s:%s", container, filename)
        client = self._get_client(container)
        downloaded = client.download_blob(filename)
        return downloaded

    def download(
        self, container: primitives.Container, blob_name: str, file_path: Optional[str]
    ) -> "None":
        """download a container file to a local path"""
        self.logger.debug("getting file from container: %s:%s", container, blob_name)
        client = self._get_client(container)
        downloaded = client.download_blob(blob_name)
        local_file = file_path if file_path else blob_name
        with open(local_file, "wb") as handle:
            handle.write(downloaded)
        self.logger.debug(
            f"downloaded blob {blob_name} from container {container} to {local_file}"
        )

    def upload_file(
        self,
        container: primitives.Container,
        file_path: str,
        blob_name: Optional[str] = None,
    ) -> None:
        """uploads a file to a container"""
        if not blob_name:
            # Default blob name to file basename. This means that the file data will be
            # written to the "root" of the container, if simulating a directory tree.
            blob_name = os.path.basename(file_path)

        self.logger.debug(
            "uploading file to container %s:%s (blob_name: %s)",
            container,
            file_path,
            blob_name,
        )

        client = self._get_client(container)
        client.upload_file(file_path, blob_name)

    def upload_dir(
        self, container: primitives.Container, dir_path: primitives.Directory
    ) -> None:
        """uploads a directory to a container"""

        self.logger.debug("uploading directory to container %s:%s", container, dir_path)

        client = self._get_client(container)
        client.upload_dir(dir_path)

    def download_dir(
        self, container: primitives.Container, dir_path: primitives.Directory
    ) -> None:
        """downloads a container to a directory"""

        self.logger.debug(
            "downloading container to directory %s:%s", container, dir_path
        )

        client = self._get_client(container)
        client.download_dir(dir_path)


class Versions(Endpoint):
    """Onefuzz Instance"""

    def check(self, exact: bool = False) -> str:
        """Compare API and CLI versions for compatibility"""
        versions = self.onefuzz.info.get().versions
        api_str = versions["onefuzz"].version
        cli_str = __version__
        if exact:
            result = semver.compare(api_str, cli_str) == 0
        else:
            api = semver.VersionInfo.parse(api_str)
            cli = semver.VersionInfo.parse(cli_str)
            result = (
                api.major > 0 and api.major == cli.major and api.minor >= cli.minor
            ) or (
                api.major == 0
                and api.major == cli.major
                and api.minor == cli.minor
                and api.patch >= cli.patch
            )
            if cli_str == "0.0.0" and not result:
                self.logger.warning(
                    "ignoring compatibility check as the CLI was installed "
                    "from git.  api: %s cli: %s",
                    api_str,
                    cli_str,
                )
                result = True

        if not result:
            raise Exception(
                "incompatible versions.  api: %s cli: %s" % (api_str, cli_str)
            )

        return "compatible"


class Info(Endpoint):
    """Information about the OneFuzz instance"""

    endpoint = "info"

    def get(self) -> responses.Info:
        """Get information about the OneFuzz instance"""
        self.logger.debug("getting info")
        return self._req_model("GET", responses.Info)


class Webhooks(Endpoint):
    """Interact with Webhooks"""

    endpoint = "webhooks"

    def get(self, webhook_id: UUID_EXPANSION) -> webhooks.Webhook:
        """get a webhook"""

        webhook_id_expanded = self._disambiguate_uuid(
            "webhook_id", webhook_id, lambda: [str(x.webhook_id) for x in self.list()]
        )

        self.logger.debug("getting webhook: %s", webhook_id_expanded)
        return self._req_model(
            "GET",
            webhooks.Webhook,
            data=requests.WebhookSearch(webhook_id=webhook_id_expanded),
        )

    def list(self) -> List[webhooks.Webhook]:
        """list webhooks"""

        self.logger.debug("listing webhooks")
        return self._req_model_list(
            "GET",
            webhooks.Webhook,
            data=requests.WebhookSearch(),
        )

    def create(
        self,
        name: str,
        url: str,
        event_types: List[events.EventType],
        *,
        secret_token: Optional[str] = None,
        message_format: Optional[webhooks.WebhookMessageFormat] = None,
    ) -> webhooks.Webhook:
        """Create a webhook"""
        self.logger.debug("creating webhook.  name: %s", name)
        return self._req_model(
            "POST",
            webhooks.Webhook,
            data=requests.WebhookCreate(
                name=name,
                url=url,
                event_types=event_types,
                secret_token=secret_token,
                message_format=message_format,
            ),
        )

    def update(
        self,
        webhook_id: UUID_EXPANSION,
        *,
        name: Optional[str] = None,
        url: Optional[str] = None,
        event_types: Optional[List[events.EventType]] = None,
        secret_token: Optional[str] = None,
        message_format: Optional[webhooks.WebhookMessageFormat] = None,
    ) -> webhooks.Webhook:
        """Update a webhook"""

        webhook_id_expanded = self._disambiguate_uuid(
            "webhook_id", webhook_id, lambda: [str(x.webhook_id) for x in self.list()]
        )

        self.logger.debug("updating webhook: %s", webhook_id_expanded)
        return self._req_model(
            "PATCH",
            webhooks.Webhook,
            data=requests.WebhookUpdate(
                webhook_id=webhook_id_expanded,
                name=name,
                url=url,
                event_types=event_types,
                secret_token=secret_token,
                message_format=message_format,
            ),
        )

    def delete(self, webhook_id: UUID_EXPANSION) -> responses.BoolResult:
        """Delete a webhook"""

        webhook_id_expanded = self._disambiguate_uuid(
            "webhook_id", webhook_id, lambda: [str(x.webhook_id) for x in self.list()]
        )

        return self._req_model(
            "DELETE",
            responses.BoolResult,
            data=requests.WebhookGet(webhook_id=webhook_id_expanded),
        )

    def ping(self, webhook_id: UUID_EXPANSION) -> events.EventPing:
        """ping a webhook"""

        webhook_id_expanded = self._disambiguate_uuid(
            "webhook_id", webhook_id, lambda: [str(x.webhook_id) for x in self.list()]
        )

        self.logger.debug("pinging webhook: %s", webhook_id_expanded)
        return self._req_model(
            "POST",
            events.EventPing,
            data=requests.WebhookGet(webhook_id=webhook_id_expanded),
            alternate_endpoint="webhooks/ping",
        )

    def logs(self, webhook_id: UUID_EXPANSION) -> List[webhooks.WebhookMessageLog]:
        """retreive webhook event log"""

        webhook_id_expanded = self._disambiguate_uuid(
            "webhook_id", webhook_id, lambda: [str(x.webhook_id) for x in self.list()]
        )

        self.logger.debug("pinging webhook: %s", webhook_id_expanded)
        return self._req_model_list(
            "POST",
            webhooks.WebhookMessageLog,
            data=requests.WebhookGet(webhook_id=webhook_id_expanded),
            alternate_endpoint="webhooks/logs",
        )


class Containers(Endpoint):
    """Interact with Onefuzz containers"""

    endpoint = "containers"

    def __init__(self, onefuzz: "Onefuzz"):
        super().__init__(onefuzz)
        self.files = Files(onefuzz)

    def get(self, name: str) -> responses.ContainerInfo:
        """Get a fully qualified SAS URL for a container"""
        self.logger.debug("get container: %s", name)
        return self._req_model(
            "GET", responses.ContainerInfo, data=requests.ContainerGet(name=name)
        )

    def create(
        self, name: str, metadata: Optional[Dict[str, str]] = None
    ) -> responses.ContainerInfo:
        """Create a storage container"""
        self.logger.debug("create container: %s", name)
        return self._req_model(
            "POST",
            responses.ContainerInfo,
            data=requests.ContainerCreate(name=name, metadata=metadata),
        )

    def delete(self, name: str) -> responses.BoolResult:
        """Delete a storage container"""
        self.logger.debug("delete container: %s", name)
        return self._req_model(
            "DELETE", responses.BoolResult, data=requests.ContainerDelete(name=name)
        )

    def list(self) -> List[responses.ContainerInfoBase]:
        """Get a list of containers"""
        self.logger.debug("list containers")
        return self._req_model_list("GET", responses.ContainerInfoBase)

    def download_job(
        self, job_id: UUID_EXPANSION, *, output: Optional[primitives.Directory] = None
    ) -> None:
        tasks = self.onefuzz.tasks.list(job_id=job_id, state=None)
        if not tasks:
            raise Exception("no tasks with job_id:%s" % job_id)

        self._download_tasks(tasks, output)

    def download_task(
        self, task_id: UUID_EXPANSION, *, output: Optional[primitives.Directory] = None
    ) -> None:
        self._download_tasks([self.onefuzz.tasks.get(task_id=task_id)], output)

    def _download_tasks(
        self, tasks: List[models.Task], output: Optional[primitives.Directory]
    ) -> None:
        to_download: Dict[str, str] = {}
        for task in tasks:
            if task.config.containers is not None:
                for container in task.config.containers:
                    info = self.onefuzz.containers.get(container.name)
                    name = os.path.join(container.type.name, container.name)
                    to_download[name] = info.sas_url

        if output is None:
            output = primitives.Directory(os.getcwd())

        for name in to_download:
            outdir = os.path.join(output, name)
            if not os.path.exists(outdir):
                os.makedirs(outdir)
            self.logger.info("downloading: %s", name)
            # security note: the src for azcopy comes from the server which is
            # trusted in this context, while the destination is provided by the
            # user
            azcopy_sync(to_download[name], outdir)


class Notifications(Endpoint):
    """Interact with models.Notifications"""

    endpoint = "notifications"

    def create(
        self,
        container: primitives.Container,
        config: models.NotificationConfig,
        *,
        replace_existing: bool = False,
    ) -> models.Notification:
        """Create a notification based on a config file"""

        config = requests.NotificationCreate(
            container=container, config=config.config, replace_existing=replace_existing
        )
        return self._req_model("POST", models.Notification, data=config)

    def create_teams(
        self, container: primitives.Container, url: str
    ) -> models.Notification:
        """Create a Teams notification integration"""

        self.logger.debug("create teams notification integration: %s", container)

        config = models.NotificationConfig(config=models.TeamsTemplate(url=url))
        return self.create(container, config)

    def create_ado(
        self,
        container: primitives.Container,
        project: str,
        base_url: str,
        auth_token: str,
        work_item_type: str,
        unique_fields: List[str],
        comment: Optional[str] = None,
        fields: Optional[Dict[str, str]] = None,
        on_dup_increment: Optional[List[str]] = None,
        on_dup_comment: Optional[str] = None,
        on_dup_set_state: Optional[Dict[str, str]] = None,
        on_dup_fields: Optional[Dict[str, str]] = None,
    ) -> models.Notification:
        """Create an Azure DevOps notification integration"""

        self.logger.debug("create ado notification integration: %s", container)

        entry = models.NotificationConfig(
            config=models.ADOTemplate(
                base_url=base_url,
                auth_token=auth_token,
                project=project,
                type=work_item_type,
                comment=comment,
                unique_fields=unique_fields,
                ado_fields=fields,
                on_duplicate=models.ADODuplicateTemplate(
                    increment=on_dup_increment or [],
                    comment=on_dup_comment,
                    ado_fields=on_dup_fields or {},
                    set_state=on_dup_set_state or {},
                ),
            ),
        )
        return self.create(container, entry)

    def delete(self, notification_id: UUID_EXPANSION) -> models.Notification:
        """Delete a notification integration"""

        notification_id_expanded = self._disambiguate_uuid(
            "notification_id",
            notification_id,
            lambda: [str(x.notification_id) for x in self.list()],
        )

        self.logger.debug(
            "create notification integration: %s",
            notification_id_expanded,
        )
        return self._req_model(
            "DELETE",
            models.Notification,
            data=requests.NotificationGet(notification_id=notification_id_expanded),
        )

    def list(
        self, *, container: Optional[List[primitives.Container]] = None
    ) -> List[models.Notification]:
        """List notification integrations"""

        self.logger.debug("listing notification integrations")
        return self._req_model_list(
            "GET",
            models.Notification,
            data=requests.NotificationSearch(container=container),
        )

    def get(self, notification_id: UUID_EXPANSION) -> List[models.Notification]:
        """Get a notification"""
        self.logger.debug("getting notification")
        return self._req_model_list(
            "GET",
            models.Notification,
            data=requests.NotificationSearch(notification_id=notification_id),
        )

    def migrate_jinja_to_scriban(
        self, dry_run: bool = False
    ) -> Union[
        responses.JinjaToScribanMigrationResponse,
        responses.JinjaToScribanMigrationDryRunResponse,
    ]:
        """Migrates all notification templates from jinja to scriban"""

        migration_endpoint = "migrations/jinja_to_scriban"
        if dry_run:
            return self._req_model(
                "POST",
                responses.JinjaToScribanMigrationDryRunResponse,
                data=requests.JinjaToScribanMigrationPost(dry_run=dry_run),
                alternate_endpoint=migration_endpoint,
            )
        else:
            return self._req_model(
                "POST",
                responses.JinjaToScribanMigrationResponse,
                data=requests.JinjaToScribanMigrationPost(dry_run=dry_run),
                alternate_endpoint=migration_endpoint,
            )


class Tasks(Endpoint):
    """Interact with tasks"""

    endpoint = "tasks"

    def delete(self, task_id: UUID_EXPANSION) -> models.Task:
        """Stop an individual task"""

        task_id_expanded = self._disambiguate_uuid(
            "task_id", task_id, lambda: [str(x.task_id) for x in self.list()]
        )

        self.logger.debug("delete task: %s", task_id_expanded)

        return self._req_model(
            "DELETE", models.Task, data=requests.TaskGet(task_id=task_id_expanded)
        )

    def get(self, task_id: UUID_EXPANSION) -> models.Task:
        """Get information about a task"""
        task_id_expanded = self._disambiguate_uuid(
            "task_id", task_id, lambda: [str(x.task_id) for x in self.list()]
        )

        self.logger.debug("get task: %s", task_id_expanded)

        return self._req_model(
            "GET", models.Task, data=requests.TaskGet(task_id=task_id_expanded)
        )

    def create_with_config(self, config: models.TaskConfig) -> models.Task:
        """Create a Task using TaskConfig"""

        return self._req_model("POST", models.Task, data=config)

    def trim_options(self, options: Optional[List[str]]) -> Optional[List[str]]:
        # Trim any surrounding whitespace to allow users to quote multiple options with extra
        # whitespace as a workaround for CLI argument parsing limitations. Trimming is needed
        # to ensure that the binary eventually parses the arguments as options.
        return [o.strip() for o in options] if options else None

    def create(
        self,
        job_id: UUID_EXPANSION,
        task_type: TaskType,
        target_exe: str,
        containers: List[Tuple[enums.ContainerType, primitives.Container]],
        *,
        analyzer_env: Optional[Dict[str, str]] = None,
        analyzer_exe: Optional[str] = None,
        analyzer_options: Optional[List[str]] = None,
        check_asan_log: bool = False,
        check_debugger: bool = True,
        check_retry_count: Optional[int] = None,
        check_fuzzer_help: Optional[bool] = None,
        expect_crash_on_failure: Optional[bool] = None,
        debug: Optional[List[enums.TaskDebugFlag]] = None,
        duration: int = 24,
        ensemble_sync_delay: Optional[int] = None,
        generator_exe: Optional[str] = None,
        generator_options: Optional[List[str]] = None,
        pool_name: primitives.PoolName,
        prereq_tasks: Optional[List[UUID]] = None,
        reboot_after_setup: bool = False,
        rename_output: bool = False,
        stats_file: Optional[str] = None,
        stats_format: Optional[enums.StatsFormat] = None,
        supervisor_env: Optional[Dict[str, str]] = None,
        supervisor_exe: Optional[str] = None,
        supervisor_input_marker: Optional[str] = None,
        supervisor_options: Optional[List[str]] = None,
        tags: Optional[Dict[str, str]] = None,
        task_wait_for_files: Optional[enums.ContainerType] = None,
        target_env: Optional[Dict[str, str]] = None,
        target_options: Optional[List[str]] = None,
        target_options_merge: bool = False,
        target_timeout: Optional[int] = None,
        target_workers: Optional[int] = None,
        target_assembly: Optional[str] = None,
        target_class: Optional[str] = None,
        target_method: Optional[str] = None,
        vm_count: int = 1,
        preserve_existing_outputs: bool = False,
        colocate: bool = False,
        report_list: Optional[List[str]] = None,
        minimized_stack_depth: Optional[int] = None,
        module_allowlist: Optional[str] = None,
        source_allowlist: Optional[str] = None,
        task_env: Optional[Dict[str, str]] = None,
    ) -> models.Task:
        """
        Create a task

        :param bool ensemble_sync_delay: Specify duration between
            syncing inputs during ensemble fuzzing (0 to disable).
        """

        self.logger.debug("creating task: %s", task_type)

        if task_type == TaskType.libfuzzer_coverage:
            self.logger.error(
                "The `libfuzzer_coverage` task type is deprecated. "
                "Please migrate to the `coverage` task type."
            )
            raise RuntimeError("`libfuzzer_coverage` task type not supported")

        job_id_expanded = self._disambiguate_uuid(
            "job_id",
            job_id,
            lambda: [str(x.job_id) for x in self.onefuzz.jobs.list()],
        )

        if tags is None:
            tags = {}

        containers_submit = [
            models.TaskContainers(name=container, type=container_type)
            for container_type, container in containers
        ]

        config = models.TaskConfig(
            containers=containers_submit,
            debug=debug,
            job_id=job_id_expanded,
            pool=models.TaskPool(count=vm_count, pool_name=pool_name),
            prereq_tasks=prereq_tasks,
            tags=tags,
            colocate=colocate,
            task=models.TaskDetails(
                analyzer_env=analyzer_env,
                analyzer_exe=analyzer_exe,
                analyzer_options=self.trim_options(analyzer_options),
                check_asan_log=check_asan_log,
                check_debugger=check_debugger,
                check_retry_count=check_retry_count,
                check_fuzzer_help=check_fuzzer_help,
                expect_crash_on_failure=expect_crash_on_failure,
                duration=duration,
                ensemble_sync_delay=ensemble_sync_delay,
                generator_exe=generator_exe,
                generator_options=self.trim_options(generator_options),
                reboot_after_setup=reboot_after_setup,
                rename_output=rename_output,
                stats_file=stats_file,
                stats_format=stats_format,
                supervisor_env=supervisor_env,
                supervisor_exe=supervisor_exe,
                supervisor_input_marker=supervisor_input_marker,
                supervisor_options=self.trim_options(supervisor_options),
                target_env=target_env,
                target_exe=target_exe,
                target_options=self.trim_options(target_options),
                target_options_merge=target_options_merge,
                target_timeout=target_timeout,
                target_workers=target_workers,
                target_assembly=target_assembly,
                target_class=target_class,
                target_method=target_method,
                type=task_type,
                wait_for_files=task_wait_for_files,
                report_list=report_list,
                preserve_existing_outputs=preserve_existing_outputs,
                minimized_stack_depth=minimized_stack_depth,
                module_allowlist=module_allowlist,
                source_allowlist=source_allowlist,
                task_env=task_env,
            ),
        )

        return self.create_with_config(config)

    def list(
        self,
        job_id: Optional[UUID_EXPANSION] = None,
        state: Optional[List[enums.TaskState]] = None,
    ) -> List[models.Task]:
        """Get information about all tasks"""
        self.logger.debug("list tasks")
        job_id_expanded: Optional[UUID] = None

        if job_id is not None:
            job_id_expanded = self._disambiguate_uuid(
                "job_id",
                job_id,
                lambda: [str(x.job_id) for x in self.onefuzz.jobs.list()],
            )

        if job_id_expanded is None and state is None:
            state = enums.TaskState.available()

        return self._req_model_list(
            "GET",
            models.Task,
            data=requests.TaskSearch(job_id=job_id_expanded, state=state),
        )


class JobContainers(Endpoint):
    """Interact with Containers used within tasks in a Job"""

    endpoint = "jobs"

    def list(
        self,
        job_id: UUID_EXPANSION,
        container_type: enums.ContainerType,
    ) -> Dict[str, List[str]]:
        """
        List the files for all of the containers of a given container type
        for the specified job
        """
        containers = set()
        tasks = self.onefuzz.tasks.list(job_id=job_id, state=[])
        for task in tasks:
            if task.config.containers is not None:
                containers.update(
                    set(
                        x.name
                        for x in task.config.containers
                        if x.type == container_type
                    )
                )

        results: Dict[str, List[str]] = {}
        for container in containers:
            results[container] = self.onefuzz.containers.files.list(container).files
        return results

    def delete(
        self,
        job_id: UUID_EXPANSION,
        *,
        only_job_specific: bool = True,
        dryrun: bool = False,
    ) -> None:
        SAFE_TO_REMOVE = [
            enums.ContainerType.crashes,
            enums.ContainerType.crashdumps,
            enums.ContainerType.setup,
            enums.ContainerType.inputs,
            enums.ContainerType.reports,
            enums.ContainerType.unique_inputs,
            enums.ContainerType.unique_reports,
            enums.ContainerType.no_repro,
            enums.ContainerType.analysis,
            enums.ContainerType.coverage,
            enums.ContainerType.readonly_inputs,
            enums.ContainerType.regression_reports,
        ]

        job = self.onefuzz.jobs.get(job_id)
        containers = set()
        to_delete = set()
        for task in self.onefuzz.jobs.tasks.list(job_id=job.job_id):
            if task.config.containers is not None:
                for container in task.config.containers:
                    containers.add(container.name)
                    if container.type not in SAFE_TO_REMOVE:
                        continue
                    elif not only_job_specific:
                        to_delete.add(container.name)
                    elif only_job_specific and (
                        self.onefuzz.utils.build_container_name(
                            container_type=container.type,
                            project=job.config.project,
                            name=job.config.name,
                            build=job.config.build,
                            platform=task.os,
                        )
                        == container.name
                    ):
                        to_delete.add(container.name)

        to_keep = containers - to_delete
        for container_name in to_keep:
            self.logger.info("not removing: %s", container_name)

        if len(to_delete) > 0:
            for container_name in to_delete:
                if dryrun:
                    self.logger.info("container would be deleted: %s", container_name)
                elif self.onefuzz.containers.delete(container_name).result:
                    self.logger.info("removed container: %s", container_name)
                else:
                    self.logger.info("container already removed: %s", container_name)
        else:
            self.logger.info("nothing to delete")


class JobTasks(Endpoint):
    """Interact with tasks within a job"""

    endpoint = "jobs"

    def list(self, job_id: UUID_EXPANSION) -> List[models.Task]:
        """List all of the tasks for a given job"""
        return self.onefuzz.tasks.list(job_id=job_id, state=[])


class Tools(Endpoint):
    """Interact with tasks within a job"""

    endpoint = "tools"

    def get(self, destination: str) -> str:
        """Download a zip file containing the agent binaries"""
        self.logger.debug("get tools")

        response = self._req_base("GET")
        path = os.path.join(destination, "tools.zip")
        open(path, "wb").write(response.content)

        return path


class Jobs(Endpoint):
    """Interact with Jobs"""

    endpoint = "jobs"

    def __init__(self, onefuzz: "Onefuzz"):
        super().__init__(onefuzz)
        self.containers = JobContainers(onefuzz)
        self.tasks = JobTasks(onefuzz)

    def delete(self, job_id: UUID_EXPANSION) -> models.Job:
        """Stop a job and all tasks that make up a job"""
        job_id_expanded = self._disambiguate_uuid(
            "job_id", job_id, lambda: [str(x.job_id) for x in self.list()]
        )

        self.logger.debug("delete job: %s", job_id_expanded)
        return self._req_model(
            "DELETE", models.Job, data=requests.JobGet(job_id=job_id_expanded)
        )

    def get(self, job_id: UUID_EXPANSION, with_tasks: bool = False) -> models.Job:
        """Get information about a specific job"""
        job_id_expanded = self._disambiguate_uuid(
            "job_id", job_id, lambda: [str(x.job_id) for x in self.list()]
        )
        self.logger.debug("get job: %s", job_id_expanded)
        job = self._req_model(
            "GET",
            models.Job,
            data=requests.JobGet(job_id=job_id_expanded, with_tasks=with_tasks),
        )
        return job

    def create_with_config(self, config: models.JobConfig) -> models.Job:
        """Create a job"""
        self.logger.debug(
            "create job: project:%s name:%s build:%s",
            config.project,
            config.name,
            config.build,
        )
        return self._req_model(
            "POST",
            models.Job,
            data=config,
        )

    def create(
        self, project: str, name: str, build: str, duration: int = 24
    ) -> models.Job:
        """Create a job"""
        return self.create_with_config(
            models.JobConfig(project=project, name=name, build=build, duration=duration)
        )

    def list(
        self,
        job_state: Optional[List[enums.JobState]] = enums.JobState.available(),
    ) -> List[models.Job]:
        """Get information about all jobs"""
        self.logger.debug("list jobs")
        return self._req_model_list(
            "GET", models.Job, data=requests.JobSearch(state=job_state)
        )


class Pool(Endpoint):
    """Interact with worker pools"""

    endpoint = "pool"

    def create(
        self,
        name: str,
        os: enums.OS,
        object_id: Optional[UUID] = None,
        *,
        unmanaged: bool = False,
        arch: enums.Architecture = enums.Architecture.x86_64,
    ) -> models.Pool:
        """
        Create a worker pool

        :param str name: Name of the worker-pool
        """
        self.logger.debug("create worker pool")
        managed = not unmanaged

        return self._req_model(
            "POST",
            models.Pool,
            data=requests.PoolCreate(
                name=name, os=os, arch=arch, managed=managed, object_id=object_id
            ),
        )

    def update(
        self,
        name: str,
        object_id: Optional[UUID] = None,
    ) -> models.Pool:
        """
        Update a worker pool

        :param str name: Name of the worker-pool
        """
        self.logger.debug("create worker pool")

        return self._req_model(
            "PATCH",
            models.Pool,
            data=requests.PoolUpdate(name=name, object_id=object_id),
        )

    def get_config(self, pool_name: primitives.PoolName) -> models.AgentConfig:
        """Get the agent configuration for the pool"""

        pool = self.get(pool_name)

        if pool.config is None:
            raise Exception("Missing AgentConfig in response")

        config = pool.config
        if not pool.managed and self.onefuzz._backend.config.authority:
            config.client_credentials = models.ClientCredentials(  # nosec
                client_id=uuid.UUID(int=0),
                client_secret="<client_secret>",
                resource=self.onefuzz._backend.config.endpoint,
                tenant=urlparse(self.onefuzz._backend.config.authority).path.strip("/"),
                multi_tenant_domain=self.onefuzz._backend.config.get_multi_tenant_domain(),
            )

        return pool.config

    def shutdown(self, name: str, *, now: bool = False) -> responses.BoolResult:
        expanded_name = self._disambiguate(
            "name", name, lambda x: False, lambda: [x.name for x in self.list()]
        )

        self.logger.debug("shutdown worker pool: %s (now: %s)", expanded_name, now)
        return self._req_model(
            "DELETE",
            responses.BoolResult,
            data=requests.PoolStop(name=expanded_name, now=now),
        )

    def get(self, name: str) -> models.Pool:
        self.logger.debug("get details on a specific pool")
        expanded_name = self._disambiguate(
            "pool name", name, lambda x: False, lambda: [x.name for x in self.list()]
        )

        return self._req_model(
            "GET", models.Pool, data=requests.PoolSearch(name=expanded_name)
        )

    def list(
        self, *, state: Optional[List[enums.PoolState]] = None
    ) -> List[models.Pool]:
        self.logger.debug("list worker pools")
        return self._req_model_list(
            "GET", models.Pool, data=requests.PoolSearch(state=state)
        )


class Node(Endpoint):
    """Interact with nodes"""

    endpoint = "node"

    def get(self, machine_id: UUID_EXPANSION) -> models.Node:
        self.logger.debug("get node: %s", machine_id)
        machine_id_expanded = self._disambiguate_uuid(
            "machine_id",
            machine_id,
            lambda: [str(x.machine_id) for x in self.list()],
        )

        return self._req_model(
            "GET", models.Node, data=requests.NodeGet(machine_id=machine_id_expanded)
        )

    def halt(self, machine_id: UUID_EXPANSION) -> responses.BoolResult:
        self.logger.debug("halt node: %s", machine_id)
        machine_id_expanded = self._disambiguate_uuid(
            "machine_id",
            machine_id,
            lambda: [str(x.machine_id) for x in self.list()],
        )

        return self._req_model(
            "DELETE",
            responses.BoolResult,
            data=requests.NodeGet(machine_id=machine_id_expanded),
        )

    def reimage(self, machine_id: UUID_EXPANSION) -> responses.BoolResult:
        self.logger.debug("reimage node: %s", machine_id)
        machine_id_expanded = self._disambiguate_uuid(
            "machine_id",
            machine_id,
            lambda: [str(x.machine_id) for x in self.list()],
        )

        return self._req_model(
            "PATCH",
            responses.BoolResult,
            data=requests.NodeGet(machine_id=machine_id_expanded),
        )

    def update(
        self,
        machine_id: UUID_EXPANSION,
        *,
        debug_keep_node: Optional[bool] = None,
    ) -> responses.BoolResult:
        self.logger.debug("update node: %s", machine_id)
        machine_id_expanded = self._disambiguate_uuid(
            "machine_id",
            machine_id,
            lambda: [str(x.machine_id) for x in self.list()],
        )

        return self._req_model(
            "POST",
            responses.BoolResult,
            data=requests.NodeUpdate(
                machine_id=machine_id_expanded,
                debug_keep_node=debug_keep_node,
            ),
        )

    def list(
        self,
        *,
        state: Optional[List[enums.NodeState]] = None,
        scaleset_id: Optional[str] = None,
        pool_name: Optional[primitives.PoolName] = None,
    ) -> List[models.Node]:
        self.logger.debug("list nodes")

        if pool_name is not None:
            pool_name = primitives.PoolName(
                self._disambiguate(
                    "name",
                    str(pool_name),
                    lambda x: False,
                    lambda: [x.name for x in self.onefuzz.pools.list()],
                )
            )

        return self._req_model_list(
            "GET",
            models.Node,
            data=requests.NodeSearch(
                scaleset_id=scaleset_id, state=state, pool_name=pool_name
            ),
        )

    def add_ssh_key(
        self, machine_id: UUID_EXPANSION, *, public_key: str
    ) -> responses.BoolResult:
        self.logger.debug("add ssh public key to node: %s", machine_id)
        machine_id_expanded = self._disambiguate_uuid(
            "machine_id",
            machine_id,
            lambda: [str(x.machine_id) for x in self.list()],
        )

        return self._req_model(
            "POST",
            responses.BoolResult,
            data=requests.NodeAddSshKey(
                machine_id=machine_id_expanded,
                public_key=public_key,
            ),
            alternate_endpoint="node/add_ssh_key",
        )


class Scaleset(Endpoint):
    """Interact with managed scaleset pools"""

    endpoint = "scaleset"

    def _expand_scaleset_machine(
        self,
        scaleset_id: str,
        machine_id: UUID_EXPANSION,
        *,
        include_auth: bool = False,
    ) -> Tuple[models.Scaleset, UUID]:
        scaleset = self.get(scaleset_id, include_auth=include_auth)

        if scaleset.nodes is None:
            raise Exception("no nodes defined in scaleset")
        nodes = scaleset.nodes

        machine_id_expanded = self._disambiguate_uuid(
            "machine_id", machine_id, lambda: [str(x.machine_id) for x in nodes]
        )
        return (scaleset, machine_id_expanded)

    def create(
        self,
        pool_name: primitives.PoolName,
        max_size: int,
        *,
        initial_size: Optional[int] = 1,
        image: Optional[str] = None,
        vm_sku: Optional[str] = "Standard_D2s_v3",
        region: Optional[primitives.Region] = None,
        spot_instances: bool = False,
        ephemeral_os_disks: bool = False,
        tags: Optional[Dict[str, str]] = None,
        min_instances: Optional[int] = 0,
        scale_out_amount: Optional[int] = 1,
        scale_out_cooldown: Optional[int] = 10,
        scale_in_amount: Optional[int] = 1,
        scale_in_cooldown: Optional[int] = 15,
    ) -> models.Scaleset:
        self.logger.debug("create scaleset")

        if tags is None:
            tags = {}

        auto_scale = requests.AutoScaleOptions(
            min=min_instances,
            max=max_size,
            default=max_size,
            scale_out_amount=scale_out_amount,
            scale_out_cooldown=scale_out_cooldown,
            scale_in_amount=scale_in_amount,
            scale_in_cooldown=scale_in_cooldown,
        )

        # Setting size=1 so that the scaleset is intialized with only 1 node.
        # The default and max are defined above
        return self._req_model(
            "POST",
            models.Scaleset,
            data=requests.ScalesetCreate(
                pool_name=pool_name,
                vm_sku=vm_sku,
                image=image,
                region=region,
                size=initial_size,
                spot_instances=spot_instances,
                ephemeral_os_disks=ephemeral_os_disks,
                tags=tags,
                auto_scale=auto_scale,
            ),
        )

    def shutdown(self, scaleset_id: str, *, now: bool = False) -> responses.BoolResult:
        self.logger.debug("shutdown scaleset: %s (now: %s)", scaleset_id, now)
        return self._req_model(
            "DELETE",
            responses.BoolResult,
            data=requests.ScalesetStop(scaleset_id=scaleset_id, now=now),
        )

    def get(self, scaleset_id: str, *, include_auth: bool = False) -> models.Scaleset:
        self.logger.debug("get scaleset: %s", scaleset_id)
        return self._req_model(
            "GET",
            models.Scaleset,
            data=requests.ScalesetSearch(
                scaleset_id=scaleset_id, include_auth=include_auth
            ),
        )

    def update(
        self, scaleset_id: str, *, size: Optional[int] = None
    ) -> models.Scaleset:
        self.logger.debug("update scaleset: %s", scaleset_id)
        return self._req_model(
            "PATCH",
            models.Scaleset,
            data=requests.ScalesetUpdate(scaleset_id=scaleset_id, size=size),
        )

    def list(
        self,
        *,
        state: Optional[List[enums.ScalesetState]] = None,
    ) -> List[models.Scaleset]:
        self.logger.debug("list scalesets")
        return self._req_model_list(
            "GET", models.Scaleset, data=requests.ScalesetSearch(state=state)
        )


class ScalesetProxy(Endpoint):
    """Interact with Scaleset Proxies (NOTE: This API is unstable)"""

    endpoint = "proxy"

    def delete(
        self,
        scaleset_id: str,
        machine_id: UUID_EXPANSION,
        *,
        dst_port: Optional[int] = None,
    ) -> responses.BoolResult:
        """Stop a proxy node"""

        (
            scaleset,
            machine_id_expanded,
        ) = self.onefuzz.scalesets._expand_scaleset_machine(scaleset_id, machine_id)

        self.logger.debug(
            "delete proxy: %s:%d %d",
            scaleset.scaleset_id,
            machine_id_expanded,
            dst_port,
        )
        return self._req_model(
            "DELETE",
            responses.BoolResult,
            data=requests.ProxyDelete(
                scaleset_id=scaleset.scaleset_id,
                machine_id=machine_id_expanded,
                dst_port=dst_port,
            ),
        )

    def reset(self, region: primitives.Region) -> responses.BoolResult:
        """Reset the proxy for an existing region"""

        return self._req_model(
            "PATCH", responses.BoolResult, data=requests.ProxyReset(region=region)
        )

    def get(
        self, scaleset_id: str, machine_id: UUID_EXPANSION, dst_port: int
    ) -> responses.ProxyGetResult:
        """Get information about a specific job"""
        (
            scaleset,
            machine_id_expanded,
        ) = self.onefuzz.scalesets._expand_scaleset_machine(scaleset_id, machine_id)

        self.logger.debug(
            "get proxy: %s:%d:%d", scaleset.scaleset_id, machine_id_expanded, dst_port
        )
        proxy = self._req_model(
            "GET",
            responses.ProxyGetResult,
            data=requests.ProxyGet(
                scaleset_id=scaleset.scaleset_id,
                machine_id=machine_id_expanded,
                dst_port=dst_port,
            ),
        )
        return proxy

    def create(
        self,
        scaleset_id: str,
        machine_id: UUID_EXPANSION,
        dst_port: int,
        *,
        duration: Optional[int] = 1,
    ) -> responses.ProxyGetResult:
        """Create a proxy"""
        (
            scaleset,
            machine_id_expanded,
        ) = self.onefuzz.scalesets._expand_scaleset_machine(scaleset_id, machine_id)

        self.logger.debug(
            "create proxy: %s:%s %d",
            scaleset.scaleset_id,
            machine_id_expanded,
            dst_port,
        )
        return self._req_model(
            "POST",
            responses.ProxyGetResult,
            data=requests.ProxyCreate(
                scaleset_id=scaleset.scaleset_id,
                machine_id=machine_id_expanded,
                dst_port=dst_port,
                duration=duration,
            ),
        )

    def list(self) -> responses.ProxyList:
        return self._req_model("GET", responses.ProxyList, data=requests.ProxyGet())


class InstanceConfigCmd(Endpoint):
    """Interact with Instance Configuration"""

    endpoint = "instance_config"

    def get(self) -> models.InstanceConfig:
        return self._req_model("GET", models.InstanceConfig)

    def update(self, config: models.InstanceConfig) -> models.InstanceConfig:
        return self._req_model(
            "POST",
            models.InstanceConfig,
            data=requests.InstanceConfigUpdate(config=config),
        )


class ValidateScriban(Endpoint):
    """Interact with Validate Scriban"""

    endpoint = "ValidateScriban"

    def post(
        self, req: requests.TemplateValidationPost
    ) -> responses.TemplateValidationResponse:
        return self._req_model("POST", responses.TemplateValidationResponse, data=req)


class Events(Endpoint):
    """Interact with Onefuzz events"""

    endpoint = "events"

    def get(self, event_id: UUID_EXPANSION) -> events.EventGetResponse:
        """Get an event's payload by id"""
        self.logger.debug("get event: %s", event_id)
        return self._req_model(
            "GET",
            events.EventGetResponse,
            data=requests.EventsGet(event_id=event_id),
        )


class Command:
    def __init__(self, onefuzz: "Onefuzz", logger: logging.Logger):
        self.onefuzz = onefuzz
        self.logger = logger


class Utils(Command):
    def namespaced_guid(
        self,
        project: str,
        name: Optional[str] = None,
        build: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> uuid.UUID:
        identifiers = [project]
        if name is not None:
            identifiers.append(name)
        if build is not None:
            identifiers.append(build)
        if platform is not None:
            identifiers.append(platform)
        return uuid.uuid5(ONEFUZZ_GUID_NAMESPACE, ":".join(identifiers))

    def build_container_name(
        self,
        *,
        container_type: enums.ContainerType,
        project: str,
        name: str,
        build: str,
        platform: enums.OS,
    ) -> primitives.Container:
        if container_type in [enums.ContainerType.setup, enums.ContainerType.coverage]:
            guid = self.namespaced_guid(
                project,
                name,
                build=build,
                platform=platform.name,
            )
        elif container_type == enums.ContainerType.regression_reports:
            guid = self.namespaced_guid(
                project,
                name,
                build=build,
            )
        else:
            guid = self.namespaced_guid(project, name)

        return primitives.Container(
            "oft-%s-%s"
            % (
                container_type.name.replace("_", "-"),
                guid.hex,
            )
        )


class Onefuzz:
    def __init__(
        self,
        config_path: Optional[str] = None,
        token_path: Optional[str] = None,
        client_secret: Optional[str] = None,
    ) -> None:
        self.logger = logging.getLogger("onefuzz")

        if client_secret is None:
            # If not explicitly provided, check the environment for a user-provided client secret.
            client_secret = self._client_secret_from_env()

        self._backend = Backend(
            config=DEFAULT,
            config_path=config_path,
            token_path=token_path,
            client_secret=client_secret,
        )
        self.containers = Containers(self)
        self.notifications = Notifications(self)
        self.tasks = Tasks(self)
        self.jobs = Jobs(self)
        self.versions = Versions(self)
        self.info = Info(self)
        self.scaleset_proxy = ScalesetProxy(self)
        self.pools = Pool(self)
        self.scalesets = Scaleset(self)
        self.nodes = Node(self)
        self.webhooks = Webhooks(self)
        self.tools = Tools(self)
        self.instance_config = InstanceConfigCmd(self)
        self.validate_scriban = ValidateScriban(self)
        self.events = Events(self)

        # these are externally developed cli modules
        self.template = Template(self, self.logger)
        self.debug = Debug(self, self.logger)
        self.status = Status(self, self.logger)
        self.utils = Utils(self, self.logger)

        self.__setup__()

    # Try to obtain a confidential client secret from the environment.
    #
    # If not set, return `None`.
    def _client_secret_from_env(self) -> Optional[str]:
        return os.environ.get(CLIENT_SECRET_ENV_VAR)

    def __setup__(
        self,
        endpoint: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        authority: Optional[str] = None,
        tenant_domain: Optional[str] = None,
    ) -> None:
        if endpoint:
            self._backend.config.endpoint = endpoint
        if authority is not None:
            self._backend.config.authority = authority
        if client_id is not None:
            self._backend.config.client_id = client_id
        if client_secret is not None:
            self._backend.client_secret = client_secret
        if tenant_domain is not None:
            self._backend.config.tenant_domain = tenant_domain

    def licenses(self) -> object:
        """Return third-party licenses used by this package"""
        data = pkgutil.get_data("onefuzz", "data/licenses.json")
        if data is None:
            raise Exception("missing licenses.json")
        return json.loads(data)

    def privacy_statement(self) -> bytes:
        """Return OneFuzz privacy statement"""
        data = pkgutil.get_data("onefuzz", "data/privacy.txt")
        if data is None:
            raise Exception("missing licenses.json")
        return data

    def logout(self) -> None:
        """Logout of Onefuzz"""
        self.logger.debug("logout")

        self._backend.logout()

    def login(self) -> str:
        """Login to Onefuzz"""

        # Rather than interacting MSAL directly, call a simple API which
        # actuates the login process
        self.info.get()

        return "succeeded"

    def config(
        self,
        endpoint: Optional[str] = None,
        enable_feature: Optional[PreviewFeature] = None,
        reset: Optional[bool] = None,
    ) -> BackendConfig:
        """Configure onefuzz CLI"""
        self.logger.debug("set config")

        if reset:
            self._backend.config = BackendConfig(endpoint="")

        if endpoint is not None:
            # The normal path for calling the API always uses the oauth2 workflow,
            # which the devicelogin can take upwards of 15 minutes to fail in
            # error cases.
            #
            # This check only happens on setting the configuration, as checking the
            # viability of the service on every call is prohibitively expensive.
            verify = self._backend.session.request("GET", endpoint)
            if verify.status_code != 401:
                self.logger.warning(
                    "This could be an invalid OneFuzz API endpoint: "
                    "Missing HTTP Authentication"
                )
            self._backend.config.endpoint = endpoint

        if enable_feature:
            self._backend.enable_feature(enable_feature.name)

        self._backend.app = None
        self._backend.save_config()
        data = self._backend.config.copy(deep=True)

        if not data.endpoint:
            self.logger.warning("endpoint not configured yet")

        return data

    def _warn_preview(self, feature: PreviewFeature) -> None:
        self.logger.warning(
            "%s are a preview-feature and may change in an upcoming release",
            feature.name,
        )


from .debug import Debug  # noqa: E402
from .status.cmd import Status  # noqa: E402
from .template import Template  # noqa: E402
