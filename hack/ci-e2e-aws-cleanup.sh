#!/bin/bash
#
# (C) Copyright Confidential Containers Contributors
# SPDX-License-Identifier: Apache-2.0
#
# Primarily used on Github workflows to remove dangling resources from AWS
#

script_dir=$(cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd)


delete_vpcs() {
  local tag_vpc="caa-e2e-test-vpc"
  read -r -a vpcs <<< "$(aws  ec2 describe-vpcs --filters Name=tag:Name,Values=$tag_vpc --query 'Vpcs[*].VpcId' --output text)"

  if [ ${#vpcs[@]} -eq 0 ]; then
    echo "There aren't VPCs to delete"
    return
  fi

  for vpc in "${vpcs[@]}"; do
    echo "aws_vpc_id=\"$vpc\"" > "$TEST_PROVISION_FILE"

    # Find related subnets
    read -r -a subnets <<< "$(aws ec2 describe-subnets --filter "Name=vpc-id,Values=$vpc" --query 'Subnets[*].SubnetId' --output text)"
    for net in "${subnets[@]}"; do
      echo "aws_vpc_subnet_id=\"$net\"" >> "$TEST_PROVISION_FILE"
    done

    # Find related security groups
    read -r -a sgs <<< "$(aws ec2 describe-security-groups --filters "Name=vpc-id,Values=$vpc" "Name=tag:Name,Values=caa-e2e-test-sg" --query 'SecurityGroups[*].GroupId' --output text)"
    for sg in "${sgs[@]}"; do
      echo "aws_vpc_sg_id=\"$sg\"" >> "$TEST_PROVISION_FILE"
    done

    # Find related route tables and internet gateways
    read -r -a rtbs <<< "$(aws ec2 describe-route-tables --filters "Name=vpc-id,Values=$vpc" "Name=tag:Name,Values=caa-e2e-test-rtb" --query 'RouteTables[*].RouteTableId' --output text)"
    for rtb in "${rtbs[@]}"; do
      echo "aws_vpc_rt_id=\"$rtb\"" >> "$TEST_PROVISION_FILE"
      read -r -a igws <<< "$(aws ec2 describe-route-tables --filter "Name=route-table-id,Values=$rtb" --query 'RouteTables[0].Routes[*].GatewayId' --output text)"
      for igw in "${igws[@]}"; do
        [ "$igw" != "local" ] && echo "aws_vpc_igw_id=\"$igw\"" >> "$TEST_PROVISION_FILE"
      done
    done

    echo "Delete VPC=$vpc"
    ./caa-provisioner-cli -action deprovision
  done
}

delete_amis() {
  local tag_ami="caa-e2e-test-img"

  read -r -a amis <<< "$(aws ec2 describe-images --owners self --filters "Name=tag:Name,Values=$tag_ami" --query 'Images[*].ImageId' --output text)"

  if [ ${#amis[@]} -eq 0 ]; then
    echo "There aren't AMIs to delete."
    return
  fi

  for ami in "${amis[@]}"; do
    echo "Deregistering AMI: $ami"
    # Find related snapshots
    snap_ids=$(aws ec2 describe-images --image-ids "$ami" --query 'Images[*].BlockDeviceMappings[*].Ebs.SnapshotId' --output text)
    aws ec2 deregister-image --image-id "$ami"
    for snap in $snap_ids; do
      echo "Deleting snapshot: $snap"
      aws ec2 delete-snapshot --snapshot-id "$snap"
    done
  done
}

# Delete resources by coco-ci tags
delete_coco_ci_resources() {
  local run_id="${GITHUB_RUN_ID:-}"
  local github_repo="${GITHUB_REPOSITORY:-}"
  local github_org="${GITHUB_REPOSITORY_OWNER:-}"
  
  echo "Cleaning up coco-ci resources..."
  
  # Base filters for coco-ci resources
  local base_filters="Name=tag:CreatedBy,Values=coco-ci"
  
  # Add repo-specific filters if available
  if [ -n "$github_repo" ]; then
    base_filters="$base_filters Name=tag:GitHubRepo,Values=$github_repo"
    echo "Filtering by GitHub repo: $github_repo"
  fi
  
  if [ -n "$github_org" ]; then
    echo "Filtering by GitHub org: $github_org"
  fi
  
  # Delete VPCs created by coco-ci
  read -r -a vpcs <<< "$(aws ec2 describe-vpcs --filters $base_filters --query 'Vpcs[*].VpcId' --output text)"
  for vpc in "${vpcs[@]}"; do
    echo "Deleting coco-ci VPC: $vpc"
    # Add VPC deletion logic here if needed
  done
  
  # Delete AMIs created by coco-ci
  read -r -a amis <<< "$(aws ec2 describe-images --owners self --filters $base_filters --query 'Images[*].ImageId' --output text)"
  for ami in "${amis[@]}"; do
    echo "Deregistering coco-ci AMI: $ami"
    snap_ids=$(aws ec2 describe-images --image-ids "$ami" --query 'Images[*].BlockDeviceMappings[*].Ebs.SnapshotId' --output text)
    aws ec2 deregister-image --image-id "$ami"
    for snap in $snap_ids; do
      echo "Deleting coco-ci snapshot: $snap"
      aws ec2 delete-snapshot --snapshot-id "$snap"
    done
  done
  
  # Delete EC2 instances created by coco-ci
  read -r -a instances <<< "$(aws ec2 describe-instances --filters $base_filters \"Name=instance-state-name,Values=running,stopped\" --query 'Reservations[*].Instances[*].InstanceId' --output text)"
  for instance in "${instances[@]}"; do
    echo "Terminating coco-ci instance: $instance"
    aws ec2 terminate-instances --instance-ids "$instance"
  done
  
  # If specific run_id provided, clean up only that run's resources
  if [ -n "$run_id" ]; then
    echo "Cleaning up resources for run ID: $run_id"
    local run_filters="Name=tag:CreatedBy,Values=coco-ci Name=tag:RunId,Values=$run_id"
    
    if [ -n "$github_repo" ]; then
      run_filters="$run_filters Name=tag:GitHubRepo,Values=$github_repo"
    fi
    
    read -r -a run_amis <<< "$(aws ec2 describe-images --owners self --filters $run_filters --query 'Images[*].ImageId' --output text)"
    for ami in "${run_amis[@]}"; do
      echo "Deregistering AMI for run $run_id: $ami"
      snap_ids=$(aws ec2 describe-images --image-ids "$ami" --query 'Images[*].BlockDeviceMappings[*].Ebs.SnapshotId' --output text)
      aws ec2 deregister-image --image-id "$ami"
      for snap in $snap_ids; do
        echo "Deleting snapshot for run $run_id: $snap"
        aws ec2 delete-snapshot --snapshot-id "$snap"
      done
    done
    
    # Clean up instances for specific run
    read -r -a run_instances <<< "$(aws ec2 describe-instances --filters $run_filters \"Name=instance-state-name,Values=running,stopped\" --query 'Reservations[*].Instances[*].InstanceId' --output text)"
    for instance in "${run_instances[@]}"; do
      echo "Terminating instance for run $run_id: $instance"
      aws ec2 terminate-instances --instance-ids "$instance"
    done
  fi
}

main() {
  TEST_PROVISION_FILE="$(pwd)/aws.properties"
  export TEST_PROVISION_FILE

  CLOUD_PROVIDER="aws"
  export CLOUD_PROVIDER

  echo "Build the caa-provisioner-cli tool"
  cd "${script_dir}/../src/cloud-api-adaptor/test/tools" || exit 1
  make

  # Clean up legacy resources by specific tags
  delete_vpcs
  delete_amis
  
  # Clean up all coco-ci tagged resources
  delete_coco_ci_resources
}

main