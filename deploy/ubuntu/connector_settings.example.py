# Example only. Replace every example.net DN/URL with your directory settings.
# Direct employee bind. No application bind password is required.
LDAP_CONFIG = {
    'URL': 'ldap://ldap.example.net:389',
    'BASE_DN': 'ou=people,dc=example,dc=net',
    'USER_LOGIN_ATTR': 'uid',
    'GROUP_BASE_DN': 'ou=groups,dc=example,dc=net',
    'REQUIRE_GROUP': 'cn=redis_farm_users,ou=groups,dc=example,dc=net',
    'TLS_MODE': 'starttls',
    'VERIFY_CERTIFICATE': True,
    'CA_CERT_FILE': None,
    'CONNECT_TIMEOUT': 5,
    'RECEIVE_TIMEOUT': 10,
}
