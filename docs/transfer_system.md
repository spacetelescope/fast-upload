# AWS architecture (1)

The transfer and validation process initiated by `mast-upload transfer` assumes a
particular set of AWS resources.

Note that `fast-upload` does _not_ include tools for provisioning, managing,
or permissioning these resources. They are prerequisites for the use of 
`mast-upload transfer`.

## **S3 (1.1)**

### **Staging Buckets (1.1.1)**

**Description**  
Temporary S3 buckets for receiving product uploads from external data providers 
prior to catalog ingest, public distribution, etc. They are general-purpose buckets, 
and all objects in these buckets will be stored in S3 Standard Tier.

**Connections / Control**  
The provider-facing managed upload tool (2.3.2) writes products to these 
buckets; ECS tasks performing validation (1.2.1) read products from these 
buckets.

### **Sample Buckets (1.1.2)**

**Description**  
Temporary S3 buckets for receiving sample product uploads from external data 
providers prior to full catalog ingest, public distribution, etc. They are 
separate from the staging buckets to make permissions management and 
bookkeeping easier. Like the staging buckets, they are general-purpose buckets 
that store objects in Standard Tier.

**Connections / Control**  
Similar to 1.1.1. 

### **Control Buckets (1.1.3)**

**Purpose**  
These buckets are essentially passive relay nodes. They store objects used 
for inter-component communication during the transfer process, including 
labels (2.1.1), indices (2.1.2), logs (2.2.1 & 2.2.2), and lock files (2.2.3).  
   
Unlike the staging and sample buckets (1.1.1 and 1.1.2), they are directory 
buckets configured for the S3 Express One Zone (SEOZ) storage tier. SEOZ, unlike 
all other S3 storage tiers, permits append-writes, which are necessary for 
acceptably performant runtime communication without large increases in system 
complexity (see 2.2 for further discussion).

**Connections / Control**  
The upload tool (2.3.2), ECS tasks responsible for validation (1.2), and the upload init 
Lambda function (1.3) read and write logs and lock files (2.2.1, 2.2.2, and 2.2.3) from and 
to these buckets. The upload tool also writes file indices (2.1.2) to these buckets; 
validation tasks read them. 

To avoid confusion, keys (i.e. filenames) of delivery-level logs (2.2.1 and 2.2.2) 
are not shared between sample and staging uploads. Lock files (2.2.3) *do*, however, 
share keys between sample and staging uploads. This is because sample and staging 
uploads should never be running simultaneously; shared lock files help the 
system detect this condition and treat it as an error.

### **System Configuration Bucket (1.1.4)**

**Purpose**  
Holding shared configuration files. 

**Connections / Control**  
This bucket provides a convenient location for hosting runtime reference and configuration 
files for other components of the system. These configuration files include at least:  
\* per-delivery labels (2.1.1)   
\* Container or task AMIs for running tasks associated with particular deliveries (1.2.1). 

**Connections / Control**  
Other components of the system read specified files from this bucket based on runtime-variable 
parameters referenced to predefined namespaces (i.e. object key naming conventions) defined
in various parts of the library.

## **ECS (1.2)**

### **Validation Tasks / Containers (1.2.1)**

**Purpose**  
These tasks encapsulate/execute the validation pipeline (2.3.1) during file transfer.

**Connections / Control**  
At provider-driven transfer initiation, the transfer tool (2.3.2) calls a Lambda function (1.3) 
that runs an ECS task on Fargate. See 1.3.1 for a detailed description of the launch process.

During the transfer process, the validation pipeline monitors transfer progress via log files on 
the associated staging or sample bucket (1.1.1 and 1.1.2), verifies their presence (and, if 
data-level validation is specified by the label, reads transferred files into memory), 
and writes results of validation back to a separate log file, which the transfer tool uses 
to provide feedback to the provider on validation status or errors. 
Detailed task-level configuration parameters may be defined either in shared configuration files (2.4.1) 
or “baked in” to tasks using ECS’s built-in task definition features. 

After a transfer is complete (or prematurely terminated) the pipeline sends a message to an SQS queue 
reporting transfer/validation status.


## **Lambda upload initialization function (1.3)**

**Purpose**  
This function provides an endpoint via which the clientside transfer tool (2.3.2) 
may request initiation of the transfer procedure. It validates these requests, 
launches an ECS validation task (1.2.1) based on configuration files (see 1.1.4), 
monitors task startup and initial data writes to verify that the upload procedure 
initialized successfully, and logs any errors or invalid conditions should process startup fail. 

**Connections / Control**  
The transfer tool invokes this function. The function runs tasks, logs the results, and 
reports success or failure to the caller.

### **Cognito (1.5)**

**Purpose**
Issuing temporary credentials the transfer tool uses to invoke the upload init Lambda function (1.3)
and write to the control, staging, and sample buckets (1.1).

**Connections/Control**
On initiation of the transfer process, the transfer tool (2.3.2) engages in an OAuth flow via Cognito
to receive temporary AWS credentials. It periodically refreshes these credentials during the transfer 
process.

# **Data / Software Artifacts (2)**

## **Data Delivery Descriptors (2.1)**

### **Dataset Labels (2.1.1)**

Each label requires separate `collection` and `provenance` identifiers with
the same meanings as their CAOM counterparts. Both appear in normalized
delivery resource names and label/index keys; the original values travel
through upload-init events, ECS launch parameters, and validation SQS reports.
See [the label schema](label_schema.md) for identifier requirements.

Every data delivery includes dataset-level metadata in a YAML-formatted “label” file 
following a fixed schema. see [label_schema.md](label_schema.md) for a 
description of this schema, and `mast_transfer_tools.labels` for its implementation.

### **File Indices (2.1.2)**

These are simple plain-text files listing relative paths and CRC32 checksums for all 
files/objects a provider intends to transfer. The`fast-upload index` command generates
these files. The transfer tool (2.3.2) uses these files  to determine what files to transfer. 
It also adds intent-to-transfer information and uploads them to a delivery’s control bucket 
(1.1.3) on transfer initiation. The validation pipeline (2.3.1) references these indices to 
determine if a transfer is complete (or if unexpected files have been transferred). 

## **Transfer Log / Control Files (2.2)**

During the transfer process, the validation pipeline (2.3.1) and transfer tool (2.3.2) communicate 
with one another primarily via plain-text objects in their delivery-specific control bucket 
(1.1.3). 

### **Upload Tool Logs (2.2.1)**

The transfer tool (2.3.2) logs its file transfer process. The validation pipeline (2.3.1) 
‘tails’ this log (by holding a ‘read head’ position in memory) and queues successfully-transferred 
files for validation. These are TSV files whose format is defined in `mast_transfer_tools.s3log.helpers`.

### **Validation Tool Logs (2.2.2)**

The validation pipeline (2.3.1) logs validation results, including rich information on failures. 
The transfer tool (2.3.2) ‘tails’ this log and uses it to provide feedback to the provider. It also keeps track of 
total validation failures and terminates the transfer if failures exceed a configurable threshold. These
are TSV files formatted similarly to the upload tool logs.

### **Lock Files (2.2.3)**

Several actors in the transfer system are intended as per-delivery ‘singletons’. Specifically, multiple 
copies of the upload tool, validation pipeline, and helper Lambda functions should never run concurrently 
for a single delivery, and AWS-level configuration cannot consistently enforce this condition. 
As an additional safeguard against failures of this type, the system uses lock files/objects on S3 as a 
synchronization primitive. Lock behaviors are defined partly in `mast_transfer_tools.utilz.locks` and partly
in code for the init lambda, transfer tool, and validation pipeline (as their lock file workflows differ slightly).

## **Software Applications (2.3)**

### **Validation Pipeline (2.3.1)**

This application examines transferred files to ensure that they are present and conform to the 
specification defined in the delivery's label. In normal system operation, the validation pipeline 
will run as an ECS task on Fargate (1.2.1) concurrently with the file transfer process. The library
includes a [Dockerfile](/Dockerfile.valpipe) and supporting scripts for a minimal working
container for such a task. The pipeline application itself is implemented in `mast_transfer_tools.server`.

`fast-upload` includes a variety of general-purpose validation operations (e.g. file format correctness and 
conformance of a binary table to a specified schema). These are implemented primarily in 
`mast_transfer_tools.validation`. Specific validation operations are configurable via attributes of 
dataset-level labels (2.1.1) (see also [the label schema](label_schema.md) and 
[the configuration guide](configuration.md)).
This includes the option to specify custom delivery and filetype-specific validation scripts.

### **Transfer Tool (2.3.2)**

This is a user-facing command-line application named `mast-upload`. Its interfaces with other system components 
during active transfer are largely described above and not repeated in this section.

`mast-upload`'s core responsibility is making transfers reliable and efficient. In normal operation, the provider will give 
it a label (2.1.1) and an index file (2.1.2). After verifying that the files referenced by the index file appear 
to exist, it will retrieve temporary credentials from Cognito (see 1.5) and begin transferring files to the sample 
or staging bucket as appropriate (1.1.1 / 1.1.2). It is capable of gracefully retrying individual failed file 
transfers and restarting “where it left off” if the process as a whole fails or is prematurely terminated.

During the transfer, it displays progress and status messages for upload and validation. It also halts the overall 
process if there are a large number of transfer or validation failures, and logs validation failures locally to assist 
the provider in correcting their data products before resuming upload.

Finally, when checksums are provided in the index, it takes primary responsibility for file integrity verification
in uploads from local files by requesting serverside verification of file checksums via the S3 API. 
For S3-to-S3 transfers, it does not consider checksums provided in an index, but rather relies entirely on 
S3 serverside verification (which is much more reliable for backend transfers). Checksum comparison failure will 
prevent a file from appearing as an object in the target bucket at all, and it will treat this like any other category 
of transfer failure.

In addition to this responsibility, `mast-upload` also provides a variety of ancillary functions useful for preparing the
transfer process, including generating index files, populating skeletal filetype descriptions in labels, and locally 
validating data against labels.

See [basic_usage.md](basic_usage.md), as well as live help for `mast-upload` and its subcommands,
for a detailed description of operation.

The `mast-upload` application is implemented primarily in `mast_transfer_tools.upload`, although its various subcommands
rely on a variety of other library components.

### **Upload Initiation Application (2.3.1)**

This is a simple application intended for deployment as an AWS Lambda function (1.3). It provides `mast-upload` with an
endpoint for requesting validation pipeline launch and initiation of the transfer process. It is implemented in 
`mast_transfer_tools.lambda_`. Various deployment strategies are viable so long as they permit Lambda to address the 
intended handler function (`mast_transfer_tools.lambda_.core.main()`), but the library includes a [Dockerfile](/Dockerfile.upload-init-lambda)
for a minimal working container deployment.
