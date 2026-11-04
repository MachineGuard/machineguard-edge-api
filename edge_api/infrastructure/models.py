from peewee import (
    AutoField,
    BooleanField,
    CharField,
    DateTimeField,
    FloatField,
    ForeignKeyField,
    IntegerField,
    Model,
    SqliteDatabase,
)

database = SqliteDatabase(None, pragmas={"foreign_keys": 1, "journal_mode": "wal"})


class BaseModel(Model):
    class Meta:
        database = database


class CalibrationProfileModel(BaseModel):
    id = AutoField()
    sensor_node_id = CharField(unique=True)
    temperature_offset = FloatField(default=0.0)
    humidity_offset = FloatField(default=0.0)
    temperature_min = FloatField()
    temperature_max = FloatField()
    humidity_min = FloatField()
    humidity_max = FloatField()
    sampling_interval_seconds = IntegerField()
    calibrated_at = DateTimeField()
    updated_at = DateTimeField()

    class Meta:
        table_name = "calibration_profiles"


class SensorReadingModel(BaseModel):
    id = AutoField()
    sensor_node_id = CharField(index=True)
    calibration_profile = ForeignKeyField(CalibrationProfileModel, backref="readings", null=True, column_name="calibration_profile_id")
    raw_temperature = FloatField()
    raw_humidity = FloatField()
    calibrated_temperature = FloatField(null=True)
    calibrated_humidity = FloatField(null=True)
    captured_at = DateTimeField(index=True)
    status = CharField()
    sample_id = CharField(null=True, max_length=100)
    captured_at_supplied = BooleanField(default=False)

    class Meta:
        table_name = "sensor_readings"


class LocalBufferEntryModel(BaseModel):
    id = AutoField()
    sensor_reading = ForeignKeyField(SensorReadingModel, unique=True, backref="buffer_entry", on_delete="CASCADE", column_name="sensor_reading_id")
    gateway_id = CharField()
    enqueued_at = DateTimeField()

    class Meta:
        table_name = "local_buffer_entries"


class OfflineNodeModel(BaseModel):
    id = AutoField()
    sensor_node_id = CharField(index=True)
    last_seen_at = DateTimeField()
    expected_sampling_interval = IntegerField()
    detected_at = DateTimeField()
    restored_at = DateTimeField(null=True)

    class Meta:
        table_name = "offline_nodes"


class SyncStateModel(BaseModel):
    """Marca temporal de la última sincronización del buffer (una fila por gateway)."""

    gateway_id = CharField(primary_key=True)
    last_sync_at = DateTimeField(null=True)

    class Meta:
        table_name = "sync_state"


ALL_MODELS = [CalibrationProfileModel, SensorReadingModel, LocalBufferEntryModel, OfflineNodeModel, SyncStateModel]


class NodeCredentialModel(BaseModel):
    profile = ForeignKeyField(CalibrationProfileModel, primary_key=True, on_delete='RESTRICT')
    credential_hash = CharField(unique=True, max_length=64)
    issued_at = DateTimeField()

    class Meta:
        table_name = 'node_credentials'


class CalibrationChangeModel(BaseModel):
    id = AutoField()
    profile = ForeignKeyField(CalibrationProfileModel, on_delete='RESTRICT')
    actor = CharField(max_length=64)
    changed_at = DateTimeField()
    old_temperature_offset = FloatField()
    old_humidity_offset = FloatField()
    new_temperature_offset = FloatField()
    new_humidity_offset = FloatField()

    class Meta:
        table_name = 'calibration_changes'


def init_database(path: str) -> None:
    if not database.is_closed():
        database.close()
    database.init(path)
    database.connect(reuse_if_open=True)
    # Additive upgrades: 0 -> 1 credentials/audit, 1 -> 2 sample identity.
    # Legacy readings retain NULL identities and all their original evidence.
    with database.atomic('IMMEDIATE'):
        version = database.pragma('user_version')
        if version > 2:
            raise ValueError('Unsupported Edge database schema version')
        database.create_tables(ALL_MODELS)
        if version == 0:
            database.create_tables([NodeCredentialModel, CalibrationChangeModel])
        if version < 2:
            columns = {column.name for column in database.get_columns('sensor_readings')}
            if 'sample_id' not in columns:
                database.execute_sql('ALTER TABLE sensor_readings ADD COLUMN sample_id TEXT')
            if 'captured_at_supplied' not in columns:
                database.execute_sql('ALTER TABLE sensor_readings ADD COLUMN captured_at_supplied INTEGER NOT NULL DEFAULT 0')
            database.execute_sql('CREATE UNIQUE INDEX IF NOT EXISTS uq_sensor_readings_node_sample '
                                 'ON sensor_readings(sensor_node_id, sample_id) WHERE sample_id IS NOT NULL')
            database.pragma('user_version', 2)
