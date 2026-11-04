import hashlib
import secrets

from .errors import ApplicationError, NotFoundError


class AuthenticationError(ApplicationError):
    status_code = 401


class ForbiddenError(ApplicationError):
    status_code = 403


class NodeCredentialService:
    """Machine credentials are independent from both Edge administration and Core."""

    def __init__(self, profiles, credentials, clock):
        self.profiles, self.credentials, self.clock = profiles, credentials, clock

    def issue(self, device_code):
        profile = self.profiles.find_by_sensor_node_id(device_code)
        if profile is None:
            raise NotFoundError('Sensor node not found')
        raw = 'mgn_' + secrets.token_urlsafe(32)
        self.credentials.save(profile.id, hashlib.sha256(raw.encode()).hexdigest(), self.clock())
        return raw

    def authenticate(self, credential):
        if not credential or len(credential) > 256:
            raise AuthenticationError('Invalid node credential')
        code = self.credentials.find_device_code(hashlib.sha256(credential.encode()).hexdigest())
        if code is None:
            raise AuthenticationError('Invalid node credential')
        return code
