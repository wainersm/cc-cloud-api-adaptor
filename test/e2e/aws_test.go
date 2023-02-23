//go:build aws

package e2e

import (
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	"testing"
)

// AWSAssert implements the CloudAssert interface.
type AWSAssert struct {
	ec2Client *ec2.Client
}

func (aa AWSAssert) HasPodVM(t *testing.T, id string) {

}

func TestAWSCreateSimplePod(t *testing.T) {

}
