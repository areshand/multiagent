#!/usr/bin/env bash
case "$1" in
  *sername*) printf '%s\n' "x-access-token" ;;
  *assword*) printf '%s\n' "${GITHUB_TOKEN:?GITHUB_TOKEN is required}" ;;
  *) exit 1 ;;
esac
