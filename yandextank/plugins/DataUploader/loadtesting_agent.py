from enum import Enum
import grpc
import logging
import yaml
import os
from pathlib import Path

from yandextank.plugins.DataUploader.ycloud import get_instance_metadata, AuthTokenProvider, build_sa_key, create_cloud_channel

try:
    from yandex.cloud.loadtesting.agent.v1 import agent_registration_service_pb2, agent_registration_service_pb2_grpc
except ImportError:
    import agent_registration_service_pb2
    import agent_registration_service_pb2_grpc

LOGGER = logging.getLogger(__name__)  # pylint: disable=C0103

METADATA_LT_CREATED_ATTR = 'loadtesting-created'
METADATA_AGENT_VERSION_ATTR = 'agent-version'
ANONYMOUS_AGENT_ID = None
RUN_IN_ENVIRONMENT_ENV = 'LOADTESTING_ENVIRONMENT'


class AgentOrigin(Enum):
    UNKNOWN = 0
    COMPUTE_LT_CREATED = 1
    COMPUTE_EXTERNAL = 2
    EXTERNAL = 3


class KnownEnvironment(Enum):
    YANDEX_COMPUTE = 'YANDEX_CLOUD_COMPUTE'


class AgentOriginError(Exception):
    pass


class LoadtestingAgent(object):
    def __init__(
        self,
        backend_url: str,
        grpc_channel: grpc.Channel,
        token_provider: AuthTokenProvider,
        agent_origin: AgentOrigin = None,
        agent_id: str = None,
        agent_id_file: str = None,
        agent_name: str = None,
        agent_version: str = None,
        folder_id: str = None,
        compute_instance_id: str = None,
        instance_lt_created: bool = False,
    ):
        self.backend_url = backend_url
        self.cloud_channel = grpc_channel
        self.token_provider = token_provider
        self.compute_instance_id = compute_instance_id
        self.instance_lt_created = bool(instance_lt_created)
        self.timeout = 30.0
        self._register_stub = agent_registration_service_pb2_grpc.AgentRegistrationServiceStub(grpc_channel)
        self.agent_id_file = agent_id_file
        self.folder_id = folder_id
        self.agent_name = agent_name
        self.agent_version = agent_version

        self.agent_origin = agent_origin or self._identify_agent_origin()
        self.agent_id = agent_id or self._identify_agent_id()

    def _identify_agent_origin(self) -> AgentOrigin:
        if not self.compute_instance_id:
            return AgentOrigin.EXTERNAL

        if self.instance_lt_created:
            return AgentOrigin.COMPUTE_LT_CREATED

        return AgentOrigin.COMPUTE_EXTERNAL

    def _identify_agent_id(self) -> str:
        if self.agent_origin == AgentOrigin.COMPUTE_LT_CREATED:
            response = self._register_stub.Register(
                agent_registration_service_pb2.RegisterRequest(
                    compute_instance_id=self.compute_instance_id),
                timeout=self.timeout,
                metadata=self._request_metadata()
            )
            LOGGER.info(f'The agent has been registered with id={response.agent_instance_id}')
            return response.agent_instance_id

        if agent_id := self._load_agent_id():
            LOGGER.info(f'Load agent_id from file {agent_id}')
            return agent_id
        elif self.is_persistent_external_agent():
            args = dict(name=self.agent_name, folder_id=self.folder_id)
            if self.agent_origin == AgentOrigin.COMPUTE_EXTERNAL:
                args.update(dict(compute_instance_id=self.compute_instance_id))
        elif self.is_anonymous_external_agent():
            return ANONYMOUS_AGENT_ID
        else:
            raise AgentOriginError('Unable to identify agent id. If you running external agent ensure folder id and service account key are provided')

        response = self._register_stub.ExternalAgentRegister(
            agent_registration_service_pb2.ExternalAgentRegisterRequest(
                **args
            ),
            timeout=self.timeout,
            metadata=self._request_metadata(),
        )
        metadata = agent_registration_service_pb2.ExternalAgentRegisterMetadata()
        response.metadata.Unpack(metadata)
        LOGGER.info(f'The agent has been registered with id={metadata.agent_instance_id}')
        return metadata.agent_instance_id

    def _request_metadata(self, additional_meta=None):
        meta = [(METADATA_AGENT_VERSION_ATTR, self.agent_version)] + list(self.token_provider.get_auth_metadata())
        if additional_meta:
            meta.extend(additional_meta)
        return meta

    def is_external(self) -> bool:
        return self.agent_origin in [AgentOrigin.EXTERNAL, AgentOrigin.COMPUTE_EXTERNAL]

    def is_anonymous_external_agent(self) -> bool:
        return self.is_external() and not bool(self.agent_name) and self.folder_id

    def is_persistent_external_agent(self) -> bool:
        return bool(self.is_external() and self.agent_name and self.folder_id)

    def store_agent_id(self):
        if not self.agent_id:
            return
        if not self.agent_id_file:
            raise ValueError('agent_id_file parameter must be set for store_agent_id')
        with open(self.agent_id_file, 'w') as f:
            f.write(self.agent_id)

    def _load_agent_id(self) -> str:
        if self.agent_id_file:
            try:
                with open(self.agent_id_file, '+r') as f:
                    return f.read(50)
            except FileNotFoundError:
                pass

        return ''


def create_loadtesting_agent(backend_url, config=None, insecure_connection=False, channel_options=None) -> LoadtestingAgent:
    if isinstance(config, str):
        config = yaml.safe_load(Path(config).read_text())

    if not config:
        config = {}

    agent_name = os.getenv('LOADTESTING_AGENT_NAME', config.get('agent_name'))
    folder_id = os.getenv('LOADTESTING_FOLDER_ID', config.get('folder_id'))
    service_account_id = os.getenv('LOADTESTING_SA_ID', config.get('service_account_id'))
    key_id = os.getenv('LOADTESTING_SA_KEY_ID', config.get('key_id'))
    private_key_file = os.getenv('LOADTESTING_SA_KEY_FILE', config.get('private_key'))
    private_key_payload = os.getenv('LOADTESTING_SA_KEY_PAYLOAD', config.get('service_account_private_key'))
    compute_instance_id, agent_version, instance_lt_created = try_identify_compute_metadata()

    sa_key = build_sa_key(
        sa_key=private_key_payload,
        sa_key_file=private_key_file,
        sa_key_id=key_id,
        sa_id=service_account_id,
    )
    token_provider = AuthTokenProvider(
        iam_endpoint=config.get("iam_token_service_url"),
        sa_key=sa_key
    )
    cloud_channel = create_cloud_channel(backend_url, insecure_connection=insecure_connection, channel_options=channel_options)
    return LoadtestingAgent(backend_url, cloud_channel, token_provider,
                            agent_id_file=config.get('agent_id_file'),
                            agent_name=agent_name,
                            folder_id=folder_id,
                            compute_instance_id=compute_instance_id,
                            agent_version=agent_version,
                            instance_lt_created=instance_lt_created)


def use_yandex_compute_metadata():
    return os.getenv(RUN_IN_ENVIRONMENT_ENV, '') == KnownEnvironment.YANDEX_COMPUTE.value


def try_identify_compute_metadata():
    if not use_yandex_compute_metadata():
        return None, None, None

    metadata = get_instance_metadata()
    if not metadata:
        return None, None, None

    compute_instance_id = metadata.get('id')
    attrs = metadata.get('attributes')
    agent_version = attrs.get(METADATA_AGENT_VERSION_ATTR, '')
    instance_lt_created = attrs.get(METADATA_LT_CREATED_ATTR, False)
    LOGGER.info(f'identified compute instance id "{compute_instance_id}", agent version "{agent_version}", lt created "{instance_lt_created}"')
    return compute_instance_id, agent_version, instance_lt_created
