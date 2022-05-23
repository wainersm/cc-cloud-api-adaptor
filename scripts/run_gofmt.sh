#!/usr/bin/env bash
#
# (C) Copyright Red Hat. 2022.
# SPDX-License-Identifier: Apache-2.0
#
# Wrapper for the gofmt command to report format issues in Go source files and
# return non-zero status.
##

usage() {
	echo "Use: $0 SOURCE_DIR1 SOURCE_DIR2 ..."
}

main () {
	if [ $# -lt 1 ]; then
		usage
		exit 1
	fi

	local sourcedirs="$@"
	local sources=($(find $sourcedirs -name '*.go'))
	local rc=0
	local cmd=""

	for file in ${sources[@]}; do
		cmd="gofmt -l -d -e "$file""
		if [ "$(eval $cmd | wc -l)" -ne 0 ]; then
			rc=1
			eval $cmd
		fi
	done
	exit $rc
}

main "$@"
