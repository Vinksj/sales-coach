#!/bin/bash
# Build and sign callcap.
#
# The microphone and system-audio permissions are keyed to the code signature.
# Ad-hoc signing ("-") pins the designated requirement to the binary's cdhash, so
# EVERY rebuild voids the grant and macOS prompts again. Signing with a
# self-signed code-signing certificate makes the requirement identity-based
# (identifier + certificate leaf) instead, and the grant survives rebuilds.
# A certificate named "callcap" exists in the login keychain and is picked up
# automatically below; override with:
#   CALLCAP_SIGN_IDENTITY="<certificate name>" ./build.sh
set -euo pipefail
cd "$(dirname "$0")"
swift build -c release 2>&1 | tail -3
mkdir -p build
cp .build/release/callcap build/callcap
identity="${CALLCAP_SIGN_IDENTITY:-}"
# Detect the certificate with find-certificate, NOT find-identity: a self-signed
# cert is CSSMERR_TP_NOT_TRUSTED, so "find-identity -p codesigning" reports
# "0 valid identities" even though codesign signs with it perfectly well.
if [ -z "$identity" ] && security find-certificate -c callcap >/dev/null 2>&1; then
    identity=callcap
fi
codesign --force --sign "${identity:--}" --identifier dev.salescoach.callcap build/callcap
# An "if", not "grep && echo": under set -e a final && list whose grep finds
# nothing returns non-zero and would fail the build on every SUCCESSFUL sign.
if codesign -d --requirements - build/callcap 2>&1 | grep -q cdhash; then
    echo "WARNING: signed ad-hoc; the permission grant will not survive the next rebuild" >&2
fi
echo "built $(pwd)/build/callcap"
