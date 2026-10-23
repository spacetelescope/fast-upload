# Configuration and customization

## Configuration values

### Parameter store

#### Network config

These values are used during transfer by the validation pipeline, upload init 
lambda, and upload client. They must be defined as key-value pairs in a JSON 
object in the AWS Parameter Store parameter given by 
`mast_transfer_tools.config.NETWORK_CONFIG_PARAMETER`. The expected format of 
the decoded `dict` is defined in `mast_transfer_tools.types.PipelineNetworkConfig`. 

Values are:

* `AVAILABILITY_ZONE_ID`: Short AWS AZ ID for directory buckets, e.g. 'use1-az4'.
* `BUCKET_STEM`: 'namespace' prefix for bucket names, e.g. 'my-fast-deployment-buckets-99'
* `CONFIG_BUCKET`: name of shared config bucket, e.g. 'fast-config-bucket-99999'
* `INIT_LAMBDA_ARN`: ARN of init lambda, e.g. 'arn:aws:lambda:us-east-1:123456789012:function:fast-upload-init-lambda'
* `LOCK_STALENESS_THRESHOLD`: Time, in seconds, after which a non-updated lock file will be treated as 'stale', e.g. 3600
* `TASK_CONFIG_PREFIX`: prefix in config bucket under which task configurations are stored, e.g. '/tasks' 

#### Resource tags

These values are used only by the upload init lambda to assign tags to the 
validation pipeline task. They must be defined as key-value pairs in a JSON 
object in the AWS Parameter Store parameter given by 
`mast_transfer_tools.config.RESOURCE_TAG_PARAMETER`. They must be valid
key-value pairs for AWS resource tags. 

### In-library configuration

These values are defined in `mast_transfer_tools.config`. 

* `LABEL_PREFIX`: prefix in config bucket under which "official" versions of labels may be found
* `MAX_TRANSFER_FAILURES`: Total number of failures possible during transfer before upload client shuts itself down 
* `NETWORK_CONFIG_PARAMETER`: AWS Parameter Store URL for network config values (see above)
* `RESOURCE_TAG_PARAMETER`: AWS Parameter Store URL for task resource tags (see above)
* `VAL_PIPE_SETTINGS`: A `dict` giving runtime thresholds for the validation pipeline. Values are:
    * `keepalive_threshold` (float): number of seconds after which the pipeline will, if it has written no other messages to the log object, write a keepalive message
    * `n_val_threads` (int): number of threads used by the validation server
    * `transfer_timeout` (float): number of seconds after which, if no new messages have been written by the upload client, the validation pipeline will shut down
    * `missing_timeout` (float): number of seconds after which, if no new messages have been written by the upload client, the validation pipeline will log a missing event but not yet shut down
    * `loop_rate` (float): seconds to delay between iterations of primary update loop
* `COGCONFIG`: a `mast_transfer_tools.types.CognitoConfiguration` object giving domain, client_id, redirect_uri, region, user_pool_id, and identity_pool_id for upload client Cognito transactions.
* `LAMBDA_CLIENT_CONFIG`: a `botocore.config.Config` object used for upload client Lambda calls. Legal values are any 
   legal values for `Config`, but high read timeout (for awaiting pipeline launch) and restricting max attempts to 1 (to prevent spurious multiple invocations) are strongly recommended.
* `VAL_PIPE_SQS_QUEUE_URL`: URL for SQS queue used by the validation pipeline to send reports.

## Validation pipeline configuration

The validation pipeline is intended to run as an ECS task on Fargate. The 
task and the container it runs are "black boxes" from the perspective of the 
library as a whole, but the default assumption is that the container will 
include the `fast-upload` library and all its dependencies, and will run a 
`server.core.ValidationSession` on launch.

### Task customization

Task definitions, clusters, VPCs, and security groups must be created at AWS 
level. However, which of these to use may be specified at runtime via 
YAML-formatted text objects in the configuration bucket under `TASK_CONFIG_PREFIX`. 
A default configuration (`default-task-config.yaml`) provides shared settings.
Collection/provenance-specific overrides use
`<collection>/<provenance>-task-config.yaml` beneath `TASK_CONFIG_PREFIX`, for
example `hlsp/tess-spoc-task-config.yaml`. Path components are lowercase with
underscores replaced by hyphens, while label metadata retains its original case.
The selected pair overrides the default values; there is no collection-wide
override layer. A complete pair-specific configuration can also be used without
a default. Missing both configurations or a malformed selected configuration
is an error. Delivery-specific overrides are not supported.

Valid parameters in task configuration objects are:

* `cluster`: short name or full ARN of cluster to run task on
* `family`: family / revision or full ARN of task definition
* `subnet_id`: VPC subnet id
* `sg_id`: Security group id

Users are most likely to want to override `family`. An alternate task definition 
could, for instance, specify a different container (perhaps including modules 
supporting custom script hooks) or additional memory for the task (for data-level 
validation on datasets with very large files).

### Building and customizing the validation container

The library includes a Dockerfile (Dockerfile.valpipe) describing a minimal 
configuration for a working validation pipeline container, along with minimal entrypoint 
(valpipe_entrypoint.sh) and handler (pipe_entry.py) scripts for the validation pipeline itself.

For some datasets, it may be desirable to add additional software to the container, 
modify the entrypoint script, etc. Example use cases include adding libraries for a 
dataset's ASDF schema or modules for custom script hooks.

Specific build steps depend on the task definition, but a typical command-line workflow 
from repository root might look like:

```
docker build -t aws-mast-fast-valpipe:latest -f Dockerfile.valpipe .
docker tag aws-mast-fast-valpipe:latest 999999999999.dkr.ecr.us-east-1.amazonaws.com/aws-mast-fast-valpipe:latest

aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 999999999999.dkr.ecr.us-east-1.amazonaws.com

docker push 999999999999.dkr.ecr.us-east-1.amazonaws.com/aws-mast-fast-valpipe:latest
```

Where `aws-mast-fast-valpipe` is the associated ECR respository name, `999999999999` is the ID 
of the AWS account that owns the ECR repository, and `us-east-1` is the region in which the ECR repository lives.

## Customizing data-level validation

By default, the validator performs data-level validation on files whose standards support 
data validation (ASDF, FITS, and Parquet). This always includes simple validation of 
conformance to standard. If `objects` is defined and any of the following properties are 
given for a DataObject, the validator also checks that property (note that not all properties 
are legal/relevant for all standards and object types; see the label schema definition for a full description):

* name 
* objtype
* ndim
* dtype
* value
* schema
* metadata

### Adding custom check hooks

Custom validation behaviors may be added without modifying core library code by defining 
the `object_check_hook` parameter of a filetype. The value of `object_check_hook` should 
be the fully-qualified name of a Python module containing a `check_file()` function. 
`check_file()` is expected to have the signature 
`(data: pyarrow.parquet.ParquetFile | asdf.AsdfFile | astropy.fits.HDUList, spec: mast_transfer_tools.labels.Filetype) -> failures: dict`. 
The specific type of `data` is determined by the file standard of the filetype. `failures` should be empty if the 
custom check passes, and should contain one or more key/value pairs describing failures if not. The specific 
format of `failures` is up to the checker, but all keys and values should be transparently YAML-serializable, 
and string/string pairs are recommended. They will be included in failure messages under the key 
`hook:HOOK_MODULE_NAME`.

If the module specified in `object_check_hook` cannot be imported in the validation environment, 
or if it does not have a `check_file()` function, the validator will treat it as an error. Note that 
in order for local and remote validation to behave identically, users must ensure that data providers 
and the validation pipeline are using the same version of all such modules.

This check is in _addition_ to the basic standard check and any checks triggered by properties defined in `objects`.

### Skipping validation steps

It is possible to skip various parts of data-level validation by populating the `skip` field of a 
filetype's `validation_options`. `skip` is a list of strings. Meaningful values are:

* names of individual built-in property checks (name, objtype, ndim, dtype, value, schema)
* 'standard' skips basic standard validation; _however_, this is only meaningful if no other checks 
   that require data-level validation are performed. This is because the validator must be able to interpret 
   the file as a valid representative of its standard to perform any other checks. 
* 'hook' skips the custom `object_check_hook`
* 'all' skips all data-level validation
