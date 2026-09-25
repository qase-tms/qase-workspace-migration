"""
Qase API Service - Handles initialization and configuration of Qase API clients.
"""
from qase.api_client_v1.api_client import ApiClient
from qase.api_client_v1.configuration import Configuration
from qase.api_client_v2.api_client import ApiClient as ApiClientV2
from qase.api_client_v2.configuration import Configuration as ConfigurationV2
import certifi

from migration.qase_http import build_retry


class QaseService:
    """Service class for interacting with Qase API."""
    
    def __init__(self, api_token: str, host: str = "qase.io", ssl: bool = True,
                 scim_token: str = None, scim_host: str = None):
        """
        Initialize Qase API clients.
        
        Args:
            api_token: Qase API token
            host: Qase host (default: "qase.io"). Drives API base URL; SCIM host defaults
                from host (see scim_host).
            ssl: Use SSL (default: True)
            scim_token: SCIM token for user/group management (optional)
            scim_host: Override SCIM host. If omitted: ``app.qase.io`` when host is
                ``qase.io`` (cloud), otherwise ``app-<host>`` (dedicated cluster).
        """
        self.api_token = api_token
        self.host = host
        self.ssl = ssl
        self.scim_token = scim_token
        if scim_host:
            self.scim_host = scim_host
        elif host == 'qase.io':
            self.scim_host = 'app.qase.io'
        else:
            # Dedicated clusters are served at app-<host>, like the API at api-<host>.
            self.scim_host = f'app-{host}'
        
        # API host: Cloud qase.io -> api.qase.io; custom domain -> api-{host}
        ssl_prefix = 'https://' if ssl else 'http://'
        delimiter = '.' if host == 'qase.io' else '-'
        
        api_host_v1 = f'{ssl_prefix}api{delimiter}{host}/v1'
        api_host_v2 = f'{ssl_prefix}api{delimiter}{host}/v2'
        
        # Configure API v1 client
        configuration = Configuration()
        configuration.api_key['TokenAuth'] = api_token
        configuration.host = api_host_v1
        configuration.ssl_ca_cert = certifi.where()
        configuration.retries = build_retry()
        self.client = ApiClient(configuration)
        
        # Configure API v2 client
        configuration_v2 = ConfigurationV2()
        configuration_v2.api_key['TokenAuth'] = api_token
        configuration_v2.host = api_host_v2
        configuration_v2.ssl_ca_cert = certifi.where()
        configuration_v2.retries = build_retry()
        self.client_v2 = ApiClientV2(configuration_v2)
        
        # Add custom header for migration
        self.client_v2.default_headers['migration'] = 'true'
        
        # Initialize SCIM client if token is provided
        if scim_token:
            from migration.utils.scim_client import QaseScimClient
            self.scim_client = QaseScimClient(scim_token, self.scim_host, ssl)
        else:
            self.scim_client = None
