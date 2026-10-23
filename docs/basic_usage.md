# Basic transfer procedure

This represents the simplest case. Further configuration is possible for most of these steps.
Please refer to `mast-upload`'s help and the [complete label schema description](label_schema.md)
for a full description of functionality and options.

## Complete the label

Once you receive a skeleton label from MAST:

1. Add filetype name patterns and standards to skeleton label (see "Completing a label" below)
2. Run `mast-upload populate-label /path/to/data/files /path/to/label_with_filetypes.yml`, where /path/to/data/files is
   the root directory of a tree that contains your files (or a representative subset)
3. Read the populated label ({collection}-{provenance}-{delivery_id}-populated.yml) and confirm that it accurately represents your
   files, or make edits if necessary
4. run `mast-upload validate-all /path/to/data/files {collection}-{provenance}-{delivery_id}-populated.yml` to confirm that the label
   passes validation against your files
5. Send the populated label to MAST for verification and possible further refinements.

## Send a sample

Once MAST has accepted your label and informed you that you are authorized to send a sample:

1. Select a representative sample of your data set and place it in its own directory tree (or manually prepare / edit an
   index file to list just those files)
2. Run `mast-upload index /path/to/sample/data/files --make-checksums --output sample-index-filename.csv` to produce an
   index for those files. Check the index to verify that it lists the files you intend to send.
3. Run
   `mast-upload transfer /path/to/sample/data/files /path/to/label_confirmed_by_mast.yml sample-index-filename.csv --sample`
   to upload those files to MAST. Wait for the transfer and validation to complete. If the transfer is interrupted or if
   you need to adjust some files to pass validation, run the command again to resume.

## Send full data set

Once your sample has passed validation and been reviewed by MAST:

1. Run `mast-upload index /path/to/all/data/files --make-checksums --output full-index-filename.csv` to produce an index
   for your full data set. Check the index to verify that it lists the files you intend to send.
2. Run `mast-upload transfer /path/to/all/data/files /path/to/label_confirmed_by_mast.yml full-index-filename.csv` to
   upload those files to MAST. Wait for the transfer and validation to complete. If the transfer is interrupted or if
   you need to adjust some files to pass validation, run the command again to resume.

# Completing a label

## Minimal label example

Here is a minimal example of a legal label. The skeleton label you receive 
from MAST will contain at least this information:

```yaml
collection: HLSP
provenance: TESS-SPOC
delivery_id: 1234
time:
    delivery_start_date: 2026-11-14
delivery_meta:
    schema_version: 0.1.0
```

However, for actual use, a "filetypes" section should almost always be added. Doing 
this is described in the next section.

## Populating a label's filetypes section

### Filenames and standards

The initial label you receive does not include descriptions of the specific kinds of files you plan to deliver. To
support validation, you will need to add these descriptions to the label. `mast-upload` includes commands to help you
build out more complete descriptions for data files, but you need to provide it with filename patterns and standards so
that it knows what files belong to what filetypes.

The simplest valid filetype description consists of a short name for the filetype, a filename pattern, and a standard
name. "filename" is a regular expression matching filenames of that filetype. It should uniquely specify the filenames
of that filetype: overlaps between filetypes are invalid. For FITS, ASDF, or Parquet files (which support data-level
validation) "standard" must be, respectively, "fits", "asdf", or "parquet". For other kinds of files (which do not
support data-level validation), "standard" can be any reasonable short identifier for the file format.

Here is a simple example of a filetypes section for a delivery that consists of a single kind of FITS file called "
observation" along with PDF documentation files. The regular expressions here can be very broad because the dataset
structure is simple:

```yaml
filetypes:
    observation:
        filename: .*\.fits
        standard: fits
    docs:
        filename: .*\.pdf
        standard: pdf
```

If you instead had two types of FITS files distinguished by "cal" and "obs" prefixes, a single parquet file named "
catalog.parquet", and PDF documentation files, you might instead write:

```yaml
filetypes:
    observation:
        filename: obs.*\.fits
        standard: fits
    calibration:
        filename: cal.*\.fits
        standard: fits
    catalog:
        filename: catalog.parquet
        standard: parquet
    docs:
        filename: .*\.pdf
        standard: pdf
```

For ASDF, FITS, and Parquet files, even simple descriptions like this are enough to allow the validator to check that
your files conform to their general format standard.

### Specifying data objects

For more detailed validation of your specific ASDF, FITS, or Parquet file layouts, you must populate the `objects`
section of a filetype. This section describes the specific data objects, like tables and arrays, that appear within a
filetype. You can write the `objects` section manually, but it is usually easier to do it with the help of
`mast-upload populate-label`.

After filling out filetypes with filename patterns as described in the previous section, run
`mast-upload populate-label <source> <label>`, where `<source>` is either the root directory of a tree under which your
files are located or a prefix on an S3 bucket under which your files are located. S3 addresses must be preceded with '
s3://'.

`populate-label` will scan files associated with each filetype in your label in order to attempt to populate each
filetype's `objects` section (if it is of a standard that supports data validation).

`<source>` may contain either your complete data set or a representative subset of files for quicker analysis.

Optionally, you may also pass `--output <output>` to `populate-label`, where `<output>` is the name of the new populated
label file you wish to write (otherwise it writes to label.yml).

## What is a 'filetype'?

A filetype is a group of files with a reasonably consistent structure: for instance, a group of FITS files with HDUs of
the same extension and data types in the same order, a group of Parquet files that share a table schema, or a group of
ASDF files with tables or arrays of the same types at the same paths.

The label schema also permits optional and variably repeated columns and objects, but `populate-label` will not
automatically describe every legal variable structure due to uncertainty about whether variability is "correct" or
unintentional. Specifically, it will only automatically detect variable numbers of repetitions when they follow the
common naming convention `<CONSISTENT_NAME>_<VARIABLE_NUMBER>`. It will detect columns or HDUs of consistent type and
HDU order but variable name. It will also detect 'repeated' ASDF nodes
defined by `<CONSISTENT_NAME>_<VARIABLE_NUMBER>` in the final element of the path.

Note that `populate-label` does not describe every single object in ASDF files; because of ASDF's flexibility, there is
too much ambiguity about what is "data" and what is "metadata", and `populate-label` does not intend to fully
recreate an ASDF schema. It describes arrays and tables of common types. If an ASDF filetype stores its data objects as
types `populate-label` does not recognize, you must manually describe those objects if you wish to validate that
filetype at object level.

## Troubleshooting

If `populate-label` fails, it is possible that you accidentally described multiple distinct filetypes as one, or that
your filetype has a structure `populate-label` will not describe. You might want to try `populate-label` on a smaller
subset of your files to help disambiguate these issues.

Additional requirements for the validator, in particular metadata that must be present, may also be manually specified
in the label, but are not automatically filled in by `populate-label`.

# Toy example

This repository includes a small 'toy' dataset and skeleton label you can use to
try the local commands of `mast-upload`.

For instance, from repository root:

`mast-upload populate-label extras/fast_toy_dataset/data/ extras/fast_toy_dataset/toy-1.yml`
will fill out extras/fast_toy_dataset/toy-1.yml and write the populated label as
toy-synthetic-1-populated.yml.

`mast-upload validate-all extras/fast_toy_dataset/data/ toy-synthetic-1-populated.yml` will then validate
the contents of extras/fast_toy_dataset/data against toy-synthetic-1-populated.yml (this command
should report that all files are valid). 

`mast-upload validate-all extras/fast_toy_dataset/bad_data/ toy-synthetic-1-populated.yml` will then validate
the contents of extras/fast_toy_dataset/bad_data against toy-synthetic-1-populated.yml (this command
should report validation failures for all files). 

`mast-upload index extras/fast_toy_dataset/data/ --make-checksums` will index the contents
of extras/fast_toy_dataset/data and write an index.csv file containing paths and checksums.

`mast-upload report-filetypes extras/fast_toy_dataset/data/ toy-synthetic-1-populated.yml` will write a
filetypes.csv file containing paths and assigned filetypes.
