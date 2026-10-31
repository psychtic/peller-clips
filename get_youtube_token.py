#!/usr/bin/env python3
"""Run this ONCE on your own computer to get the YouTube refresh token.

  pip install google-auth-oauthlib
  python get_youtube_token.py client_secret.json

A browser window opens. Sign in with the Google account that owns your YouTube
channel and press Allow. The three values to copy into GitHub secrets are
printed at the end.
"""
import json
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

if len(sys.argv) != 2:
    sys.exit("usage: python get_youtube_token.py client_secret.json")

flow = InstalledAppFlow.from_client_secrets_file(
    sys.argv[1], scopes=["https://www.googleapis.com/auth/youtube.upload"])
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

print("\nAdd these as GitHub secrets:\n")
print("YT_CLIENT_ID     =", creds.client_id)
print("YT_CLIENT_SECRET =", creds.client_secret)
print("YT_REFRESH_TOKEN =", creds.refresh_token)
