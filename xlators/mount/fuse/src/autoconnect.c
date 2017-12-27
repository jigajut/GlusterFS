#include <stdio.h>


#include "nvme-ioctl.h"


int main()
{
	struct nvmf_disc_rsp_page_hdr *log = NULL;
	char *dev_name;
	int instance, numrec = 0, ret;

	instance = add_ctrl(argstr);
	return 0;
}
