import os
import sys
sys.path.append(os.path.normpath(os.path.join(os.path.dirname(__file__),
                                              os.pardir, os.pardir)))

import datetime

from airflow import DAG
from airflow.operators.dummy_operator import DummyOperator
from airflow.operators.python_operator import PythonOperator

from gleams import config
from gleams.feature import feature
from gleams.massivekb import massivekb


default_args = {
    'owner': 'gleams',
    'depends_on_past': False,
    'start_date': datetime.datetime(2019, 1, 1),
    'schedule_interval': datetime.timedelta(weeks=1),
    'email': ['wbittremieux@ucsd.edu'],
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 3,
    'retry_delay': datetime.timedelta(minutes=5)
}

with DAG('gleams', default_args=default_args) as dag:
    t_metadata = PythonOperator(
        task_id='convert_massivekb_metadata',
        python_callable=massivekb.convert_massivekb_metadata,
        op_args={'massivekb_task_id': config.massivekb_task_id}
    )
    t_split_feat = PythonOperator(
        task_id='split_metadata_train_val_test',
        python_callable=massivekb.split_metadata_train_val_test,
        op_args={'massivekb_task_id': config.massivekb_task_id,
                 'val_ratio': config.val_ratio,
                 'test_ratio': config.test_ratio,
                 'rel_tol': config.split_ratio_tolerance}
    )
    t_download = PythonOperator(
        task_id='download_massivekb_peaks',
        python_callable=massivekb.download_massivekb_peaks,
        op_args={'massivekb_task_id': config.massivekb_task_id}
    )
    t_pairs_pos = PythonOperator(
        task_id='generate_massivekb_pairs_positive',
        python_callable=massivekb.generate_massivekb_pairs_positive,
        op_args={'massivekb_task_id': config.massivekb_task_id}
    )
    t_pairs_neg = PythonOperator(
        task_id='generate_massivekb_pairs_negative',
        python_callable=massivekb.generate_massivekb_pairs_negative,
        op_args={'massivekb_task_id': config.massivekb_task_id,
                 'mz_tolerance': config.pair_mz_tolerance}
    )
    t_enc_feat = PythonOperator(
        task_id='convert_massivekb_peaks_to_features',
        python_callable=feature.convert_massivekb_peaks_to_features,
        op_args={'massivekb_task_id': config.massivekb_task_id}
    )
    t_feat_combine = DummyOperator(
        task_id='combine_features'
    )
    t_train = DummyOperator(
        task_id='train_model'
    )

    t_metadata >> t_split_feat >> [t_pairs_pos, t_pairs_neg]
    t_download >> t_enc_feat >> t_feat_combine
    [t_pairs_pos, t_pairs_neg, t_feat_combine] >> t_train
