from peewee import (
    AutoField,
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


def init_database(path: str) -> None:
    if not database.is_closed():
        database.close()
    database.init(path)
    database.connect(reuse_if_open=True)
    database.create_tables(ALL_MODELS)
