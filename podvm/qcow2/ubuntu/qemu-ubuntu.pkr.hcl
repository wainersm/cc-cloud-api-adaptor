locals {
   machine_type = "${var.os_arch}" == "x86_64" && "${var.is_uefi}" ? "q35" : "${var.machine_type}"
   use_pflash = "${var.os_arch}" == "x86_64" && "${var.is_uefi}" ? "true" : "false"
   firmware = "${var.os_arch}" == "x86_64" && "${var.is_uefi}" ? "${var.uefi_firmware}"  : ""
   qemuargs  =  "${var.os_arch}" == "x86_64" && "${var.is_uefi}" ? (
              [
	       ["-m", "${var.memory}"],
	       ["-smp", "cpus=${var.cpus}"],
	       ["-cdrom", "${var.cloud_init_image}"],
	       ["-serial", "mon:stdio"]
	      ]
	     ) : (
	      [
	       ["-device", "virtio-blk,drive=virtio-drive,id=virtio-disk0,bootindex=1"],
	       ["-drive", "file=${var.output_directory}/${var.qemu_image_name},if=none,cache=writeback,discard=ignore,format=qcow2,id=virtio-drive"],
	       ["-device", "virtio-scsi"],
	      ["-drive", "file=${var.cloud_init_image},format=raw,if=none,id=c1"],
	      ["-device", "scsi-cd,drive=c1"],
	      ["-m", "${var.memory}"],
	      ["-smp", "cpus=${var.cpus}"],
	      ["-serial", "mon:stdio"]
	      ]
	     )
}

source "qemu" "ubuntu" {
  boot_command      = ["<enter>"]
  disk_compression  = true
  disk_image        = true
  disk_size         = "${var.disk_size}"
  format            = "qcow2"
  headless          = true
  iso_checksum      = "${var.cloud_image_checksum}"
  iso_url           = "${var.cloud_image_url}"
  output_directory  = "${var.output_directory}"
  qemuargs          = "${local.qemuargs}"
  ssh_password      = "${var.ssh_password}"
  ssh_port          = 22
  ssh_username      = "${var.ssh_username}"
  ssh_wait_timeout  = "300s"
  boot_wait         = "${var.boot_wait}"
  vm_name           = "${var.qemu_image_name}"
  shutdown_command  = "sudo shutdown -h now"
  qemu_binary       = "${var.qemu_binary}"
  machine_type      = "${local.machine_type}"
  use_pflash        = "${local.use_pflash}"
  firmware          = "${local.firmware}"
}

build {
  sources = ["source.qemu.ubuntu"]

  provisioner "shell-local" {
    command = "tar cf toupload/files.tar files"
  }

  provisioner "file" {
    source      = "./toupload"
    destination = "/tmp/"
  }

  provisioner "shell" {
    inline = [
      "cd /tmp && tar xf toupload/files.tar",
      "rm toupload/files.tar"
    ]
  }

  provisioner "file" {
    source      = "qcow2/copy-files.sh"
    destination = "~/copy-files.sh"
  }

  provisioner "shell" {
    remote_folder = "~"
    inline = [
      "sudo bash ~/copy-files.sh"
    ]
  }

  provisioner "file" {
    source      = "qcow2/misc-settings.sh"
    destination = "~/misc-settings.sh"
  }

  provisioner "shell" {
    remote_folder = "~"
    environment_vars = [
        "CLOUD_PROVIDER=${var.cloud_provider}",
        "PODVM_DISTRO=${var.podvm_distro}",
	]
    inline = [
      "sudo -E bash ~/misc-settings.sh"
    ]
  }

}
