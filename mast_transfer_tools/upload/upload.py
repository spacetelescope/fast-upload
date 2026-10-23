"""Actually uploading files to MAST."""

from pathlib import Path
import sys
import time

import pandas as pd

from hostess.aws.s3 import Bucket

from .client import UploadClient
from ..labels import Label
from ..types import TransferType
from ..utilz import name_reference as names


def upload(
    label: Label,
    transfer_type: TransferType,
    file_index: pd.DataFrame,
    source: Path | Bucket,
) -> None:
    """
    Handler function for `UploadClient`, intended primarily to be invoked by
    `mast-upload transfer`.
    """
    import mast_transfer_tools.config as conf

    client = UploadClient(
        collection=label.collection,
        provenance=label.provenance,
        delivery_id=label.delivery_id,
        transfer_type=transfer_type,
        file_index=file_index,
        source=source,
        n_threads=4,
        lambda_client_config=conf.LAMBDA_CLIENT_CONFIG,
    )

    if client.done:
        # fatal error during init. client will have printed a useful message
        sys.exit()

    client.connect()
    if client.done:
        # this could be either a fatal error during connection or a refusal
        # to initiate transfer because all files are already uploaded and
        # validated. Again, the client will have printed a useful message.
        client.quit()
        sys.exit()

    # so the validation pipeline can check the client's label against the
    # reference version -- we don't want confusion based on MAST and the
    # provider thinking they're transferring different sorts of files!
    client.control_bucket.put(
        label.as_text(),
        names.label_key(label.collection, label.provenance, label.delivery_id),
        literal_str=True,
    )

    client.write_index()
    client.initiate_transfer()

    # it might be better at some point to move this polling loop into
    # UploadClient.

    last_n_complete = 0
    try:
        while not client.done:
            client.update()
            if not (client.transfer_complete or client.done):
                while client.transfer_next_file() is not None:
                    client.transfer_next_file()
            if client.logger.elapsed() > 10:
                client.logger.write(
                    category="keepalive", ref="self", status="ok"
                )
            if client.n_complete != last_n_complete:
                client.cmessage(
                    f"{client.n_complete}/{len(client.file_list)} complete",
                    "info",
                )
                last_n_complete = client.n_complete
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    except Exception as ex:
        client.crash(ex)

    if not client.state == "quit":
        client.quit()
