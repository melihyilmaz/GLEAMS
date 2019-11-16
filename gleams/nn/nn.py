import logging
import os
from typing import List

import joblib
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from gleams import config
from gleams.feature import encoder, feature
from gleams.nn import data_generator, embedder


logger = logging.getLogger('gleams')


def _get_feature_split():
    return (config.num_precursor_features,
            config.num_precursor_features + config.num_fragment_features)


def train_nn(filename_model: str, filename_feat_train: str,
             filename_train_pairs_pos: str, filename_train_pairs_neg: str,
             filename_feat_val: str, filename_val_pairs_pos: str,
             filename_val_pairs_neg: str):
    """
    Train the GLEAMS neural network.

    Parameters
    ----------
    filename_model : str
        The file name where the model will be saved.
    filename_feat_train : str
        The file name of the training NumPy binary feature file.
    filename_train_pairs_pos : str
        The file name of the positive training pair indexes.
    filename_train_pairs_neg : str
        The file name of the negative training pair indexes.
    filename_feat_val : str
        The file name of the validation NumPy binary feature file.
    filename_val_pairs_pos : str
        The file name of the positive validation pair indexes.
    filename_val_pairs_neg : str
        The file name of the negative validation pair indexes.
    """
    # Build the embedder model.
    model_dir = os.path.dirname(filename_model)
    if not os.path.isdir(model_dir):
        os.makedirs(model_dir)
    logger.info('Compile the GLEAMS neural network')
    emb = embedder.Embedder(
        config.num_precursor_features, config.num_fragment_features,
        config.num_ref_spectra, config.lr, filename_model)
    emb.build()

    # Train the embedder.
    logger.info('Train the GLEAMS neural network')
    # Choose appropriate hyperparameters based on the number of GPUs that are
    # being used.
    num_gpus = embedder._get_num_gpus()
    if num_gpus == 0:
        raise RuntimeError('No GPU found')
    batch_size = config.batch_size * num_gpus
    steps_per_epoch = config.steps_per_epoch // num_gpus
    if num_gpus > 1:
        logger.info('Adjusting the batch size to %d and the steps per epoch to'
                    ' %d for running on %d GPUs', batch_size, steps_per_epoch,
                    num_gpus)
    train_generator = data_generator.PairSequence(
        filename_feat_train, filename_train_pairs_pos,
        filename_train_pairs_neg, batch_size, _get_feature_split(),
        config.max_num_pairs_train)
    val_generator = data_generator.PairSequence(
        filename_feat_val, filename_val_pairs_pos, filename_val_pairs_neg,
        batch_size, _get_feature_split(), config.max_num_pairs_val, False)
    emb.train(train_generator, steps_per_epoch, config.num_epochs,
              val_generator)

    logger.info('Save the trained GLEAMS neural network')
    emb.save()

    logger.info('Training completed')


def embed(metadata_filename: str, model_filename: str) -> None:
    """
    Embed all spectra in the peak directory using the given GLEAMS model.

    Parameters
    ----------
    metadata_filename : str
        Metadata file with references to all datasets that should be embedded.
        Should be a Parquet file.
    model_filename : str
        The GLEAMS model filename.
    """
    embed_dir = os.path.join(os.environ['GLEAMS_HOME'], 'data', 'embed',
                             'dataset')
    if not os.path.isdir(embed_dir):
        os.makedirs(embed_dir)

    metadata = pd.read_parquet(
        metadata_filename, columns=['dataset', 'filename']).drop_duplicates()

    enc = encoder.MultipleEncoder([
        encoder.PrecursorEncoder(
            config.num_bits_precursor_mz, config.precursor_mz_min,
            config.precursor_mz_max, config.num_bits_precursor_mass,
            config.precursor_mass_min, config.precursor_mass_max,
            config.precursor_charge_max),
        encoder.FragmentEncoder(
            config.fragment_mz_min, config.fragment_mz_max, config.bin_size),
        encoder.ReferenceSpectraEncoder(
            config.ref_spectra_filename, config.fragment_mz_min,
            config.fragment_mz_max, config.fragment_mz_tol,
            config.num_ref_spectra)
    ])

    logger.debug('Load the stored GLEAMS neural network')
    emb = embedder.Embedder(
        config.num_precursor_features, config.num_fragment_features,
        config.num_ref_spectra, config.lr, model_filename)
    emb.load()
    num_gpus = embedder._get_num_gpus()
    if num_gpus == 0:
        raise RuntimeError('No GPU found')
    batch_size = config.batch_size * num_gpus

    logger.info('Embed all peak files for metadata file %s', metadata_filename)
    dataset_total = metadata['dataset'].nunique()
    for dataset_i, (dataset, peak_filenames) in enumerate(
            metadata.groupby('dataset', sort=False)['filename'], 1):
        filename_scans = os.path.join(embed_dir, f'{dataset}.parquet')
        filename_embedding = os.path.join(embed_dir, f'{dataset}.npy')
        if (os.path.isfile(filename_scans) and
                os.path.isfile(filename_embedding)):
            continue
        logger.info('Process dataset %s [%3d/%3d] (%d files)', dataset,
                    dataset_i, dataset_total, len(peak_filenames))
        scans, encodings, num_cached = [], [], 0
        for filename, file_scans, file_encodings in joblib.Parallel(
                n_jobs=-1, backend='multiprocessing')(
                joblib.delayed(feature._peaks_to_features)
                (dataset, filename, None, enc) for filename in peak_filenames):
            if file_scans is not None and len(file_scans) > 0:
                scans.extend([(filename, scan) for scan in file_scans])
                encodings.extend(file_encodings)
                # Store embeddings every 10M spectra to a temporary file to
                # avoid running out of memory.
                if len(encodings) >= 10000000:
                    num_cached += 1
                    _embed_and_save(
                        encodings, batch_size, emb, filename_embedding.replace(
                            '.npy', f'_{num_cached}.npy'))
                    encodings = []
        if len(scans) > 0:
            scans_df = pd.DataFrame(scans, columns=['filename', 'scan'])
            scans_df['scans'] = scans_df['scans'].astype(np.int64)
            pq.write_table(pa.Table.from_pandas(scans_df), filename_scans)
            # No temporary files were used, so just embed and write all
            # encodings.
            if num_cached == 0:
                _embed_and_save(encodings, batch_size, emb, filename_embedding)
            # Temporary files were used, so embed and write the final encodings
            # and then merge all temporary files.
            else:
                num_cached += 1
                _embed_and_save(
                    encodings, batch_size, emb, filename_embedding.replace(
                        '.npy', f'_{num_cached}.npy'))
                embeddings = [np.load(filename_embedding.replace(
                                  '.npy', f'_{i}.npy'), mmap_mode='r')
                              for i in range(1, num_cached + 1)]
                np.save(filename_embedding, np.vstack(embeddings))
                for i in range(1, num_cached + 1):
                    os.remove(filename_embedding.replace('.npy', f'_{i}.npy'))


def _embed_and_save(encodings: List[np.ndarray], batch_size: int,
                    emb: embedder.Embedder, filename: str) -> None:
    """
    Embed the given encodings and save them as a NumPy file.

    Parameters
    ----------
    encodings : List[np.ndarray]
        A list of encoding arrays to be embedded.
    batch_size : int
        The number of encodings to embed simultaneously.
    emb : embedder.Embedder
        The embedder neural network to embed the encodings.
    filename : str
        File name to store the embedded encodings.
    """
    logger.debug('Embed the spectrum encodings and save to file %s', filename)
    encodings_generator = data_generator.EncodingsSequence(
        encodings, batch_size, _get_feature_split())
    np.save(filename, np.vstack(emb.embed(encodings_generator)))


def combine_embeddings(metadata_filename: str) -> None:
    """
    Combine embedding files for multiple datasets into a single embedding file.

    If the combined embedding file already exists it will _not_ be recreated.

    Parameters
    ----------
    metadata_filename : str
        Embeddings for all datasets included in the metadata will be combined.
        Should be a Parquet file.
    """
    embed_dir = os.path.join(os.environ['GLEAMS_HOME'], 'data', 'embed')
    embed_filename = os.path.join(embed_dir, os.path.splitext(
        os.path.basename(metadata_filename))[0].replace('metadata_', 'embed_'))
    if (os.path.isfile(f'{embed_filename}.npy') and
            os.path.isfile(f'{embed_filename}.parquet')):
        return
    datasets = pd.read_parquet(
        metadata_filename, columns=['dataset'])['dataset'].unique()
    logger.info('Combine embeddings for metadata file %s containing %d '
                'datasets', metadata_filename, len(datasets))
    embeddings, indexes = [], []
    for i, dataset in enumerate(datasets, 1):
        logger.debug('Append dataset %s [%3d/%3d]', dataset, i, len(datasets))
        dataset_embeddings_filename = os.path.join(
            embed_dir, 'dataset', f'{dataset}.npy')
        dataset_index_filename = os.path.join(
            embed_dir, 'dataset', f'{dataset}.parquet')
        if (not os.path.isfile(dataset_embeddings_filename) or
                not os.path.isfile(dataset_index_filename)):
            logger.warning('Missing embeddings for dataset %s, skipping...',
                           dataset)
        else:
            embeddings.append(np.load(dataset_embeddings_filename))
            dataset_table = pq.read_table(dataset_index_filename)
            indexes.append(dataset_table.add_column(0, pa.Column.from_array(
                'dataset', pa.array([dataset] * dataset_table.num_rows))))
    np.save(f'{embed_filename}.npy', np.vstack(embeddings))
    pq.write_table(pa.concat_tables(indexes), f'{embed_filename}.parquet')
