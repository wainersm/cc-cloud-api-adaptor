package userdata

import (
	"context"
	"os"

	. "github.com/confidential-containers/cloud-api-adaptor/src/cloud-api-adaptor/pkg/paths"
	"github.com/klauspost/cpuid/v2"
)

func isAzureVM() bool {
	return cpuid.CPU.HypervisorVendorID == cpuid.MSVM
}

func isAWSVM(ctx context.Context) bool {
	if cpuid.CPU.HypervisorVendorID != cpuid.KVM {
		return false
	}
	_, err := imdsGet(ctx, AWSImdsUrl, false, nil)
	return err == nil
}

func isGCPVM(ctx context.Context) bool {
	if cpuid.CPU.HypervisorVendorID != cpuid.KVM {
		return false
	}
	_, err := imdsGet(ctx, GcpImdsUrl, false, []kvPair{{"Metadata-Flavor", "Google"}})
	return err == nil
}

func hasUserDataFile() bool {
	paths := []string{
		UserDataPath,
		DockerUserDataPath,
	}

	for _, path := range paths {
		if _, err := os.Stat(path); err == nil {
			return true // Found at least one existing file
		}
	}
	return false // Neither file exists

}
