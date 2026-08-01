from atlas_host_operations.controller import phase_job_main
from atlas_host_operations.lifecycle import ProvisioningPhase

if __name__ == "__main__":
    raise SystemExit(phase_job_main(ProvisioningPhase.RESERVE))
