#!/bin/sh
# entrypoint.sh
echo "epsonds" > /etc/sane.d/dll.conf
echo "net ${SCANNER_IP:?set SCANNER_IP}" > /etc/sane.d/epsonds.conf
exec "$@"