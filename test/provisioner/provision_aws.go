//go:build aws

// (C) Copyright Confidential Containers Contributors
// SPDX-License-Identifier: Apache-2.0

package provisioner

import (
	"context"
	"fmt"
	"github.com/aws/aws-sdk-go-v2/aws"
	awsConfig "github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/aws/aws-sdk-go-v2/service/iam"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	s3types "github.com/aws/aws-sdk-go-v2/service/s3/types"
	log "github.com/sirupsen/logrus"
	"io"
	"os"
	"os/exec"
	"sigs.k8s.io/e2e-framework/pkg/envconf"
	"time"
)

func init() {
	newProvisionerFunctions["aws"] = NewAWSProvisioner
}

//S3Bucket Represents an S3 bucket where the podvm image should be uploaded
type S3Bucket struct {
	Client *s3.Client
	Name   string // Bucket name
	Key    string // Object key
}

//AMIImage Represents an AMI image
type AMIImage struct {
	Client          *ec2.Client
	Description     string // Image description
	DiskDescription string // Disk description
	DiskFormat      string // Disk format
	EBSSnapshotId   string // EBS disk snapshot ID
	ID              string // AMI image ID
	RootDeviceName  string // Root device name
}

// AWSProvisioner implements the CloudProvision interface.
type AWSProvisioner struct {
	AwsConfig aws.Config
	iamClient *iam.Client
	ec2Client *ec2.Client
	s3Client  *s3.Client
	Bucket    *S3Bucket
	Image     *AMIImage
}

//NewAWSProvisioner Instantiates a new AWS provisioner
func NewAWSProvisioner(properties map[string]string) (CloudProvisioner, error) {
	cfg, err := awsConfig.LoadDefaultConfig(context.TODO())
	if err != nil {
		log.Error("Failed to load AWS config")
		return nil, err
	}

	if properties["aws_region"] != "" {
		cfg.Region = properties["aws_region"]
	}

	ec2Client := ec2.NewFromConfig(cfg)
	return &AWSProvisioner{
		AwsConfig: cfg,
		iamClient: iam.NewFromConfig(cfg),
		ec2Client: ec2.NewFromConfig(cfg),
		s3Client:  s3.NewFromConfig(cfg),
		Bucket: &S3Bucket{
			Client: s3.NewFromConfig(cfg),
			Name:   "peer-pods-tests",
			Key:    "", // To be defined when the file is uploaded
		},
		Image: NewAMIImage(ec2Client, properties),
	}, nil
}

func (aws *AWSProvisioner) CreateCluster(ctx context.Context, cfg *envconf.Config) error {
	return nil
}

func (aws *AWSProvisioner) CreateVPC(ctx context.Context, cfg *envconf.Config) error {
	return nil
}

func (aws *AWSProvisioner) DeleteCluster(ctx context.Context, cfg *envconf.Config) error {
	return nil
}

func (aws *AWSProvisioner) DeleteVPC(ctx context.Context, cfg *envconf.Config) error {
	return nil
}

func (aws *AWSProvisioner) GetProperties(ctx context.Context, cfg *envconf.Config) map[string]string {
	return make(map[string]string)
}

func (a *AWSProvisioner) UploadPodvm(imagePath string, ctx context.Context, cfg *envconf.Config) error {
	// FROM https://github.com/openshift/sandboxed-containers-operator/blob/dev-preview/podvm/raw-to-ami.sh

	// AWS EC2 image-import does not support qcow2 so convert the image to raw format.
	imageRawFile, err := os.CreateTemp("", "podvm.*.raw")
	imageRawPath := imageRawFile.Name()
	imageRawFile.Close()
	if err != nil {
		return err
	}
	defer func() {
		_, err := os.Stat(imageRawPath)
		if err == nil {
			os.Remove(imageRawPath)
		}
	}()

	log.Infof("Convert qcow2 image to raw")
	if err = ConvertQcow2ToRaw(imagePath, imageRawPath); err != nil {
		return err
	}

	// Create the S3 bucket
	log.Infof("Create bucket %s", a.Bucket.Name)
	if err = a.Bucket.createBucket(); err != nil {
		return err
	}

	// Create the vmimport role
	log.Infof("Create vmimport service role")
	if err = createVmimportServiceRole(ctx, a.iamClient, a.Bucket.Name); err != nil {
		return err
	}

	// Upload raw image to S3
	log.Infof("Upload image %s to S3 bucket %s", imageRawPath, a.Bucket.Name)
	if err = a.Bucket.uploadLargeFile(imageRawPath); err != nil {
		return err
	}

	log.Infof("Import disk snapshot")
	if err = a.Image.importEBSSnapshot(a.Bucket); err != nil {
		return err
	}

	//TODO: Define image name based on disk name.
	log.Infof("Register image")
	err = a.Image.registerImage("podvm-ubuntu")
	if err != nil {
		return err
	}

	return nil
}

//createBucket Creates the S3 bucket
func (b *S3Bucket) createBucket() error {
	// No harm creating a bucket that already exist.
	_, err := b.Client.CreateBucket(context.TODO(), &s3.CreateBucketInput{
		Bucket: &b.Name,
	})
	if err != nil {
		return err
	}

	// Set the bucket policy
	policy := fmt.Sprintf(`{
		"Version": "2012-10-17",
		"Statement": [{
			"Sid": "AllowVMIE",
			"Effect": "Allow",
			"Principal": { "Service": "vmie.amazonaws.com" },
			"Action": ["s3:GetBucketLocation", "s3:GetObject", "s3:ListBucket" ],
			"Resource": ["arn:aws:s3:::%s", "arn:aws:s3:::%s/*"]}]
	}`, b.Name, b.Name)

	if _, err = b.Client.PutBucketPolicy(context.TODO(), &s3.PutBucketPolicyInput{
		Bucket: &b.Name,
		Policy: &policy,
	}); err != nil {
		return err
	}

	return nil
}

//uploadFile Uploads a file to the bucket
func (b *S3Bucket) uploadFile(filepath string) error {
	file, err := os.Open(filepath)
	if err != nil {
		return err
	}
	defer file.Close()

	// Generate key from the file name.
	stat, err := file.Stat()
	if err != nil {
		return err
	}
	key := stat.Name()
	defer func() {
		if err == nil {
			b.Key = key
		}
	}()
	// TODO: generate md5sum base-64 encoded of the file content.
	_, err = b.Client.PutObject(context.TODO(), &s3.PutObjectInput{
		Bucket: aws.String(b.Name),
		Key:    aws.String(key),
		Body:   file,
	})
	if err != nil {
		return err
	}

	return nil
}

//createVmimportServiceRole Creates the vmimport service role as required to use the VM snaphot import feature.
//  For further details see https://docs.aws.amazon.com/vm-import/latest/userguide/required-permissions.html
func createVmimportServiceRole(ctx context.Context, client *iam.Client, bucketName string) error {
	const roleName = "vmimport"

	_, err := client.GetRole(context.TODO(), &iam.GetRoleInput{
		RoleName: aws.String(roleName),
	})
	if err == nil {
		// The role exists, do nothing
		return nil
	}

	// Create the service role
	trustPolicy := fmt.Sprintf(`{
		"Version":"2012-10-17",
		"Statement":[
			{
				"Effect":"Allow",
				"Principal":{ "Service":"vmie.amazonaws.com" },
				"Action": "sts:AssumeRole",
				"Condition":{"StringEquals":{"sts:Externalid":"vmimport"}}
			}
		]
	}`)

	if _, err = client.CreateRole(context.TODO(), &iam.CreateRoleInput{
		AssumeRolePolicyDocument: aws.String(trustPolicy),
		RoleName:                 aws.String(roleName),
	}); err != nil {
		return err
	}

	// Set the role policy
	rolePolicy := fmt.Sprintf(`{
		"Version":"2012-10-17",
		"Statement":[
			{
				"Effect":"Allow",
				"Action":["s3:GetBucketLocation","s3:GetObject","s3:ListBucket"],
				"Resource":["arn:aws:s3:::%s","arn:aws:s3:::%s/*"]
			},
			{
				"Effect":"Allow",
				"Action":["ec2:ModifySnapshotAttribute","ec2:CopySnapshot","ec2:RegisterImage","ec2:Describe*"],
				"Resource":"*"
			}
		]
	}`, bucketName, bucketName)

	if _, err = client.PutRolePolicy(context.TODO(), &iam.PutRolePolicyInput{
		PolicyDocument: aws.String(rolePolicy),
		PolicyName:     aws.String("vmimport"),
		RoleName:       aws.String(roleName),
	}); err != nil {
		return err
	}

	return nil
}

func NewAMIImage(client *ec2.Client, properties map[string]string) *AMIImage {
	return &AMIImage{
		Client:          client,
		Description:     "Peer Pod VM image",
		DiskDescription: "Peer Pod VM disk",
		DiskFormat:      "RAW",
		EBSSnapshotId:   "", // To be defined when the snapshot is created
		ID:              properties["podvm_aws_ami_id"],
		RootDeviceName:  "/dev/xvda",
	}
}

//importEBSSnapshot Imports the disk image into the EBS
func (i *AMIImage) importEBSSnapshot(bucket *S3Bucket) error {
	// Create the import snapshot task
	importSnapshotOutput, err := i.Client.ImportSnapshot(context.TODO(), &ec2.ImportSnapshotInput{
		Description: aws.String("Peer Pod VM disk snapshot"),
		DiskContainer: &ec2types.SnapshotDiskContainer{
			Description: aws.String(i.DiskDescription),
			Format:      aws.String(i.DiskFormat),
			UserBucket: &ec2types.UserBucket{
				S3Bucket: aws.String(bucket.Name),
				S3Key:    aws.String(bucket.Key),
			},
		},
	})
	if err != nil {
		return err
	}

	//taskId := *importSnapshotOutput.ImportTaskId
	describeTasksInput := &ec2.DescribeImportSnapshotTasksInput{
		ImportTaskIds: []string{*importSnapshotOutput.ImportTaskId},
	}

	// Wait the import task to finish
	waiter := ec2.NewSnapshotImportedWaiter(i.Client)
	if err = waiter.Wait(context.TODO(), describeTasksInput, time.Minute*3); err != nil {
		return err
	}

	// Finally get the snapshot ID
	describeTasks, err := i.Client.DescribeImportSnapshotTasks(context.TODO(), describeTasksInput)
	if err != nil {
		return err
	}
	taskDetail := describeTasks.ImportSnapshotTasks[0].SnapshotTaskDetail
	i.EBSSnapshotId = *taskDetail.SnapshotId

	return nil
}

//purgeImage Deregisters the AMI image and delete the associated EBS snapshot
func (i *AMIImage) purgeImage() error {
	// Deregister the image
	if i.ID != "" {
		i.Client.DeregisterImage(context.TODO(), &ec2.DeregisterImageInput{
			ImageId: aws.String(i.ID),
		})
	}

	// Delete the snapshot
	if i.EBSSnapshotId != "" {
		i.Client.DeleteSnapshot(context.TODO(), &ec2.DeleteSnapshotInput{
			SnapshotId: aws.String(i.EBSSnapshotId),
		})
	}

	return nil
}

//registerImage Registers an AMI image
func (i *AMIImage) registerImage(imageName string) error {

	if i.EBSSnapshotId == "" {
		return fmt.Errorf("EBS Snapshot ID not found\n")
	}

	result, err := i.Client.RegisterImage(context.TODO(), &ec2.RegisterImageInput{
		Name:         aws.String(imageName),
		Architecture: ec2types.ArchitectureValuesX8664,
		BlockDeviceMappings: []ec2types.BlockDeviceMapping{{
			DeviceName: aws.String(i.RootDeviceName),
			Ebs: &ec2types.EbsBlockDevice{
				DeleteOnTermination: aws.Bool(true),
				SnapshotId:          aws.String(i.EBSSnapshotId),
			},
		}},
		Description:        aws.String(i.Description),
		EnaSupport:         aws.Bool(true),
		RootDeviceName:     aws.String(i.RootDeviceName),
		VirtualizationType: aws.String("hvm"),
	})
	if err != nil {
		return err
	}

	// Save the AMI ID
	i.ID = *result.ImageId
	return nil
}

//uploadFilePart Uploads part of a file
func (b *S3Bucket) uploadFilePart(file *os.File, fileSize int64, offset int64, partSize int64, partInput *s3.UploadPartInput) (string, error) {
	// In case the part size is bigger than the remaining file, recalculate the part size.
	if partSize > (fileSize - offset) {
		partSize = fileSize - offset
	}
	partInput.ContentLength = partSize

	// Create the part out of the original file
	filePart, err := os.CreateTemp("", "file-part")
	if err != nil {
		return "", err
	}
	filePartName := filePart.Name()
	defer func() {
		filePart.Close()
		os.Remove(filePartName)
	}()

	// Copy from the part begin
	if _, err = file.Seek(offset, 0); err != nil {
		return "", err
	}
	io.CopyN(filePart, file, partSize)
	filePart.Close()
	if filePart, err = os.Open(filePartName); err != nil {
		return "", err
	}
	partInput.Body = filePart

	// Upload the part file
	uploadPartOutput, err := b.Client.UploadPart(context.TODO(), partInput)
	if err != nil {
		return "", err
	}

	return *uploadPartOutput.ETag, nil
}

//uploadFile2 Uploads large files (>5GB) using the S3 multi-part upload API
func (b *S3Bucket) uploadLargeFile(filepath string) error {
	var (
		fileSize              int64
		uploadPartInput       *s3.UploadPartInput
		abortMultipartUpload  bool = true // Conservatively abort by default
		multipartUploadOutput *s3.CreateMultipartUploadOutput
		partNumber            int32 = 1 // Counter must start from number one.
		offset                int64 = 0 //
		completedParts        []s3types.CompletedPart
	)

	// Copy the file in chunks of 100 MiB
	partSize := int64(100 * (1 << 20))
	file, err := os.Open(filepath)
	if err != nil {
		return err
	}
	defer file.Close()

	stat, err := file.Stat()
	if err != nil {
		return err
	}
	fileSize = stat.Size()

	fmt.Printf("File size: %d\n", fileSize)
	// TODO: generate key from file name
	key := stat.Name()
	defer func() {
		if err == nil {
			b.Key = key
		}
	}()

	multipartUploadOutput, err = b.Client.CreateMultipartUpload(context.TODO(), &s3.CreateMultipartUploadInput{
		Bucket: aws.String(b.Name),
		Key:    aws.String(key),
		// TODO: check upload integrity
		//ChecksumAlgorithm: s3types.ChecksumAlgorithmSha256,
		ContentType: aws.String("application/octet-stream"),
	})
	if err != nil {
		return err
	}
	defer func() {
		if abortMultipartUpload {
			fmt.Println("Aborting upload")
			b.Client.AbortMultipartUpload(context.TODO(), &s3.AbortMultipartUploadInput{
				Bucket:   aws.String(b.Name),
				Key:      aws.String(key),
				UploadId: multipartUploadOutput.UploadId,
			})
		}
	}()

	uploadPartInput = &s3.UploadPartInput{
		Bucket:        aws.String(b.Name),
		Key:           aws.String(key),
		PartNumber:    partNumber,
		UploadId:      multipartUploadOutput.UploadId,
		Body:          nil,
		ContentLength: partSize,
	}

	totalParts := 1 + (fileSize-1)/partSize

	for (fileSize - offset) >= partSize {
		fmt.Printf("Upload part %d of %d (offset=%d, size=%d)\n", partNumber, totalParts, offset, partSize)
		uploadPartInput.PartNumber = partNumber
		etag, err := b.uploadFilePart(file, fileSize, offset, partSize, uploadPartInput)
		if err != nil {
			return err
		}

		completedParts = append(completedParts, s3types.CompletedPart{
			ETag:       aws.String(etag),
			PartNumber: partNumber,
		})

		partNumber++
		offset += partSize
	}

	if fileSize-offset > 0 {
		fmt.Printf("Upload part %d of %d (offset=%d, size=%d)\n", partNumber, totalParts, offset, fileSize-offset)

		uploadPartInput.PartNumber = partNumber
		etag, err := b.uploadFilePart(file, fileSize, offset, partSize, uploadPartInput)
		if err != nil {
			return err
		}

		completedParts = append(completedParts, s3types.CompletedPart{
			ETag:       aws.String(etag),
			PartNumber: partNumber,
		})
	}

	if _, err = b.Client.CompleteMultipartUpload(context.TODO(), &s3.CompleteMultipartUploadInput{
		Bucket:   aws.String(b.Name),
		Key:      aws.String(key),
		UploadId: multipartUploadOutput.UploadId,
		//ChecksumSHA256: aws.String(""),
		MultipartUpload: &s3types.CompletedMultipartUpload{
			Parts: completedParts,
		},
	}); err != nil {
		return err
	}

	fmt.Println("Completed multi-part upload")
	abortMultipartUpload = false

	return nil
}

// ConvertQcow2ToRaw Converts an qcow2 image to raw. Requires `qemu-img` installed.
func ConvertQcow2ToRaw(qcow2 string, raw string) error {
	cmd := exec.Command("qemu-img", "convert", "-O", "raw", qcow2, raw)
	cmd.Stderr = os.Stderr
	err := cmd.Run()
	if err != nil {
		return err
	}

	return nil
}
