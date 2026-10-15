# Example only. Replace every example.net DN/URL with your directory settings.
# WARNING: plain LDAP does not encrypt employee passwords on the LDAP link.
# Direct employee bind. No application bind password is required.
LDAP_CONFIG = {
    'URL': 'ldap://ldap.example.net:389',
    'BASE_DN': 'ou=people,dc=example,dc=net',
    'USER_LOGIN_ATTR': 'uid',
    'GROUP_BASE_DN': 'ou=groups,dc=example,dc=net',
    'REQUIRE_GROUP': 'cn=redis_farm_users,ou=groups,dc=example,dc=net',
    'TLS_MODE': 'plain',
    'ALLOW_PLAINTEXT': True,
    'CONNECT_TIMEOUT': 5,
    'RECEIVE_TIMEOUT': 10,
}
