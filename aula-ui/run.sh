#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -e

bashio::log.info "Starting Aula UI add-on..."

export AULA_OPTIONS_FILE="/data/options.json"
export AULA_DATA_DIR="/data"

if bashio::config.has_value 'log_level'; then
    bashio::log.level "$(bashio::config 'log_level')"
fi

cd /opt/aula-ui
exec python3 -m aula_ui
