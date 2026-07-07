# Authentication

The authentication system verifies user identity before granting access.

## Login Flow

Users log in with a username and password. On success the server issues a
signed JWT access token and a refresh token.

## OAuth

We support OAuth 2.0 for third-party login providers such as Google and GitHub.
The `authenticate()` function handles the token exchange.

## Token Refresh

Access tokens expire after 15 minutes. Use the refresh token to obtain a new
access token without re-entering credentials.
