"""
SCIM API Client for Qase workspace migration.
Handles user and group creation via SCIM API.
"""
import logging
import requests

from migration import qase_http
import time
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)


class QaseScimClient:
    """SCIM API client for Qase workspace."""
    
    def __init__(self, scim_token: str, scim_host: str = "app.qase.io", ssl: bool = True):
        """
        Initialize SCIM client.
        
        Args:
            scim_token: SCIM token for target workspace
            scim_host: SCIM host (default: "app.qase.io")
            ssl: Use SSL (default: True)
        """
        self.scim_token = scim_token
        self.scim_host = scim_host
        self.ssl = ssl
        
        ssl_prefix = 'https://' if ssl else 'http://'
        self.base_url = f'{ssl_prefix}{scim_host}/scim/v2'
        
        self.headers = {
            'Authorization': f'Bearer {scim_token}',
            'Content-Type': 'application/scim+json',
            'Accept': 'application/scim+json'
        }
    
    def _request_with_retry(self, method: str, endpoint: str, max_retries: int = 3, 
                           backoff_factor: float = 1.0, **kwargs) -> requests.Response:
        """
        Make HTTP request with retry logic for rate limiting.
        
        Args:
            method: HTTP method (GET, POST, PATCH, etc.)
            endpoint: API endpoint (relative to base_url)
            max_retries: Maximum number of retries
            backoff_factor: Backoff multiplier
            **kwargs: Additional arguments for requests
        
        Returns:
            Response object
        """
        url = f'{self.base_url}/{endpoint}'
        
        for attempt in range(max_retries + 1):
            try:
                response = qase_http.request(
                    method=method,
                    url=url,
                    headers=self.headers,
                    timeout=60,
                    **kwargs
                )
                
                # Success or client error (4xx except 429)
                if response.status_code < 400 or (response.status_code >= 400 and response.status_code != 429):
                    return response
                
                # Rate limited - retry with backoff
                if response.status_code == 429:
                    if attempt < max_retries:
                        wait_time = backoff_factor * (2 ** attempt)
                        logger.info(f"Rate limited, retrying in {wait_time}s...")
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.error(f"Rate limit exceeded after {max_retries} retries")
                        return response
                
                # Other errors
                return response
                
            except Exception as e:
                if attempt == max_retries:
                    logger.error(f"Request failed after {max_retries} retries: {e}")
                    raise
                wait_time = backoff_factor * (2 ** attempt)
                logger.info(f"Request exception, retrying in {wait_time}s: {e}")
                time.sleep(wait_time)
        
        raise Exception(f"Request failed after {max_retries} retries")
    
    def get_users(self, limit: int = 100, offset: int = 0) -> Dict[str, Any]:
        """
        Get all users from target workspace.
        
        Args:
            limit: Number of users per page
            offset: Offset for pagination
        
        Returns:
            Response dictionary with Resources list
        """
        params = {
            'count': limit,
            'startIndex': offset + 1  # SCIM uses 1-based indexing
        }
        
        response = self._request_with_retry('GET', 'Users', params=params)
        
        if response.status_code == 200:
            return response.json()
        else:
            logger.error(f"Failed to get users: {response.status_code} - {response.text}")
            return {'Resources': [], 'totalResults': 0}
    
    def get_all_users(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Get all users from target workspace (paginated).
        
        Args:
            limit: Number of users per page
        
        Returns:
            List of user dictionaries
        """
        all_users = []
        offset = 0
        
        while True:
            response_data = self.get_users(limit=limit, offset=offset)
            users = response_data.get('Resources', [])
            all_users.extend(users)
            
            total_results = response_data.get('totalResults', 0)
            if len(all_users) >= total_results or len(users) < limit:
                break
            
            offset += limit
        
        return all_users
    
    def create_user(self, email: str, first_name: str, last_name: str, 
                   role_title: str = "Member", is_active: bool = True) -> Optional[str]:
        """
        Create a user in target workspace.
        
        Args:
            email: User email address
            first_name: User first name
            last_name: User last name
            role_title: User role title (default: "Member")
            is_active: Whether user should be active
        
        Returns:
            User ID if created successfully, None otherwise
        """
        payload = {
            'schemas': ['urn:ietf:params:scim:schemas:core:2.0:User'],
            'userName': email,
            'name': {
                'familyName': last_name,
                'givenName': first_name
            },
            'active': is_active,
            'roleTitle': role_title
        }
        
        response = self._request_with_retry('POST', 'Users', json=payload)
        
        # SCIM API can return 200 or 201 for successful creation
        if response.status_code in [200, 201]:
            response_data = response.json()
            user_id = response_data.get('id')
            if user_id:
                logger.info(f"Created user: {email} (ID: {user_id})")
                return user_id
            else:
                logger.warning(f"User created but no ID in response: {email}")
                return None
        elif response.status_code == 409:
            # User already exists
            logger.warning(f"User already exists: {email}")
            # Try to get existing user by email
            existing_users = self.get_all_users()
            for user in existing_users:
                if user.get('userName', '').lower() == email.lower():
                    return user.get('id')
            return None
        else:
            logger.error(f"Failed to create user {email}: {response.status_code} - {response.text}")
            return None
    
    def get_groups(self, limit: int = 100, offset: int = 0) -> Dict[str, Any]:
        """
        Get all groups from target workspace.
        
        Args:
            limit: Number of groups per page
            offset: Offset for pagination
        
        Returns:
            Response dictionary with Resources list
        """
        params = {
            'count': limit,
            'startIndex': offset + 1  # SCIM uses 1-based indexing
        }
        
        response = self._request_with_retry('GET', 'Groups', params=params)
        
        if response.status_code == 200:
            return response.json()
        else:
            logger.error(f"Failed to get groups: {response.status_code} - {response.text}")
            return {'Resources': [], 'totalResults': 0}
    
    def get_all_groups(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Get all groups from target workspace (paginated).
        
        Args:
            limit: Number of groups per page
        
        Returns:
            List of group dictionaries
        """
        all_groups = []
        offset = 0
        
        while True:
            response_data = self.get_groups(limit=limit, offset=offset)
            groups = response_data.get('Resources', [])
            all_groups.extend(groups)
            
            total_results = response_data.get('totalResults', 0)
            if len(all_groups) >= total_results or len(groups) < limit:
                break
            
            offset += limit
        
        return all_groups
    
    def create_group(self, group_name: str) -> Optional[str]:
        """
        Create a group in target workspace.
        
        Args:
            group_name: Name of the group
        
        Returns:
            Group ID if created successfully, None otherwise
        """
        payload = {
            'schemas': ['urn:ietf:params:scim:schemas:core:2.0:Group'],
            'displayName': group_name
        }
        
        response = self._request_with_retry('POST', 'Groups', json=payload)
        
        # SCIM API can return 200 or 201 for successful creation
        if response.status_code in [200, 201]:
            response_data = response.json()
            group_id = response_data.get('id')
            if group_id:
                logger.info(f"Created group: {group_name} (ID: {group_id})")
                return group_id
            else:
                logger.warning(f"Group created but no ID in response: {group_name}")
                return None
        elif response.status_code == 409:
            # Group already exists - try to find it
            logger.warning(f"Group already exists: {group_name}")
            existing_groups = self.get_all_groups()
            for group in existing_groups:
                if group.get('displayName', '').lower() == group_name.lower():
                    return group.get('id')
            return None
        else:
            logger.error(f"Failed to create group {group_name}: {response.status_code} - {response.text}")
            return None
    
    def add_users_to_group(self, group_id: str, user_ids: List[str]) -> bool:
        """
        Add multiple users to a group.
        
        Args:
            group_id: Group ID
            user_ids: List of user IDs to add
        
        Returns:
            True if successful, False otherwise
        """
        if not user_ids:
            return True
        
        payload = {
            'schemas': ['urn:ietf:params:scim:api:messages:2.0:PatchOp'],
            'Operations': [
                {
                    'op': 'Add',
                    'path': 'members',
                    'value': [
                        {'value': user_id} for user_id in user_ids
                    ]
                }
            ]
        }
        
        response = self._request_with_retry('PATCH', f'Groups/{group_id}', json=payload)
        
        if response.status_code in [200, 204]:
            logger.info(f"Added {len(user_ids)} users to group {group_id}")
            return True
        else:
            logger.error(f"Failed to add users to group {group_id}: {response.status_code} - {response.text}")
            return False
