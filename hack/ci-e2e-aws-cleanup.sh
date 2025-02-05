#!/bin/bash
#
# (C) Copyright Confidential Containers Contributors
# SPDX-License-Identifier: Apache-2.0
#
# Primarily used on Github workflows to remove dangling resources from AWS
#

script_dir=$(cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd)

tag_vpc="caa-e2e-test-vpc"

read -r -a vpcs <<< "$(aws  ec2 describe-vpcs --filters Name=tag:Name,Values=$tag_vpc --query 'Vpcs[*].VpcId')"

echo "DEBUG VPCS: ${vpcs[@]}"
aws  ec2 describe-vpcs --filters Name=tag:Name,Values=$tag_vpc --query 'Vpcs[*].VpcId'

if [ ${#vpcs[@]} -eq 0 ]; then
    echo "There aren't VPCs to delete. Exiting..."
    exit 0
fi

# Build the caa-provisioner-cli tool
cd "${script_dir}/../src/cloud-api-adaptor/test/tools" || exit 1
make

export TEST_PROVISION_FILE="$(pwd)/aws.properties"

for vpc in "${vpcs[@]}"; do
    echo "aws_vpc_id=\"$vpc\"" > "$TEST_PROVISION_FILE"

    # Find related subnets
    read -r -a subnets <<< "$(aws ec2 describe-subnets --filter "Name=vpc-id,Values=$vpc" --query 'Subnets[*].SubnetId')"
    for net in "${subnets[@]}"; do
        echo "aws_vpc_subnet_id=\"$net\"" >> "$TEST_PROVISION_FILE"
    done

    # Find related security groups
    read -r -a sgs <<< "$(aws ec2 describe-security-groups --filters "Name=vpc-id,Values=$vpc" "Name=tag:Name,Values=caa-e2e-test-sg" --query 'SecurityGroups[*].GroupId')"
    for sg in "${sgs[@]}"; do
        echo "aws_vpc_sg_id=\"$sg\"" >> "$TEST_PROVISION_FILE"
    done

    # Find related route tables and internet gateways
    read -r -a rtbs <<< "$(aws ec2 describe-route-tables --filters "Name=vpc-id,Values=$vpc" "Name=tag:Name,Values=caa-e2e-test-rtb" --query 'RouteTables[*].RouteTableId')"
    for rtb in "${rtbs[@]}"; do
      echo "aws_vpc_rt_id=\"$rtb\"" >> "$TEST_PROVISION_FILE"
      read -r -a igws <<< "$(aws ec2 describe-route-tables --filter "Name=route-table-id,Values=$rtb" --query 'RouteTables[0].Routes[*].GatewayId')"
      for igw in "${igws[@]}"; do
        [ "$igw" != "local" ] && echo "aws_vpc_igw_id=\"$igw\"" >> "$TEST_PROVISION_FILE"
      done
    done

    echo "Delete VPC=$vpc"
    ./caa-provisioner-cli -action deprovision
done
