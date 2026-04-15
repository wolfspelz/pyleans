using System.Reflection;
using System.Text;
using Microsoft.Extensions.Logging;
using Orleans.Storage;

public abstract class GrainStorageBase : IGrainStorage
{
    private static readonly char[] KeyInvalidChars = ['/', '\\', '#', '?', '|', '[', ']', '{', '}', '<', '>', '$', '^', '&', '%', '+', '\''];

    protected static readonly DateTime MinEdmTime = new (1601, 1, 1, 0, 0, 0, DateTimeKind.Utc);

    protected static string KeySafeName(string name)
    {
        var safeName = new StringBuilder();
        foreach (var t in name) {
            if (KeyInvalidChars.Contains(t)) {
                safeName.Append(Convert.ToInt32(t).ToString("X2"));
            } else {
                safeName.Append(t);
            }
        }
        return safeName.ToString();
    }

    protected readonly ILogger _logger;

    protected GrainStorageBase(string name, ILoggerFactory loggerFactory) {
        _logger = loggerFactory.CreateLogger($"{GetType().FullName}.{name}");
    }

    protected IDictionary<string, object?> GetEntityPropertiesFromState(object state)
    {
        var entityProps = new Dictionary<string, object?>();
        Type targetType = state.GetType();
        IEnumerable<FieldInfo> targetFields = targetType.GetFields()
            .Where(field => field.Attributes.HasFlag(FieldAttributes.Public));
        foreach (FieldInfo fieldInfo in targetFields) {
            var key = fieldInfo.Name;
            Type fieldType = Nullable.GetUnderlyingType(fieldInfo.FieldType) ?? fieldInfo.FieldType;
            object? value = fieldInfo.GetValue(state);
            object? valueForStorage;

            if (value is null
                || fieldType == typeof(long)
                || fieldType == typeof(int)
                || fieldType == typeof(double)
                || fieldType == typeof(float)
                || fieldType == typeof(bool)
                || fieldType == typeof(string)
            ) {
                valueForStorage = value;
            } else if (fieldType == typeof(DateTime)) {
                DateTime valueDt = (DateTime)value;
                if (valueDt < MinEdmTime) {
                    valueDt = MinEdmTime;
                }
                valueForStorage = DateTime.SpecifyKind(valueDt, DateTimeKind.Utc);
            } else {
                valueForStorage = value.ToString() ?? string.Empty;
                _logger.LogError($"Unhandled field type={fieldInfo.FieldType}. Using string as fallback");
            }

            entityProps.Add(key, valueForStorage);
        }
        return entityProps;
    }

    protected void AddEntityPropertiesToState(IReadOnlyDictionary<string, object?> entityProps, object state)
    {
        Type targetType = state.GetType();
        IEnumerable<FieldInfo> targetFields = targetType.GetFields()
            .Where(field => field.Attributes.HasFlag(FieldAttributes.Public))
            .Where(field => entityProps.ContainsKey(field.Name));
        foreach (FieldInfo targetField in targetFields) {
            string key = targetField.Name;
            Type fieldType = Nullable.GetUnderlyingType(targetField.FieldType) ?? targetField.FieldType;
            bool fieldIsNullable = new NullabilityInfoContext().Create(targetField).ReadState == NullabilityState.Nullable;
            object? value = entityProps[key];
            Type? entityType = value?.GetType();

            if (fieldIsNullable && value is null) {
                targetField.SetValue(state, null);
            } else if (fieldType == typeof(long)) {
                if (value is null) {
                    targetField.SetValue(state, 0L);
                } else if (value is long or int) {
                    targetField.SetValue(state, value);
                } else {
                    _logger.LogError($"Type mismatch fieldType={fieldType} entityType={entityType}");
                }
            } else if (fieldType == typeof(int)) {
                if (value is null) {
                    targetField.SetValue(state, 0);
                } else if (value is int) {
                    targetField.SetValue(state, value);
                } else {
                    _logger.LogError($"Type mismatch fieldType={fieldType} entityType={entityType}");
                }
            } else if (fieldType == typeof(double) || fieldType == typeof(float)) {
                if (value is null) {
                    targetField.SetValue(state, 0D);
                } else if (value is double or float) {
                    targetField.SetValue(state, value);
                } else {
                    _logger.LogError($"Type mismatch fieldType={fieldType} entityType={entityType}");
                }
            } else if (fieldType == typeof(bool)) {
                if (value is null) {
                    targetField.SetValue(state, false);
                } else if (value is bool valueBool) {
                    targetField.SetValue(state, valueBool);
                } else {
                    _logger.LogError($"Type mismatch fieldType={fieldType} entityType={entityType}");
                }
            } else if (fieldType == typeof(string)) {
                if (value is null) {
                    targetField.SetValue(state, string.Empty);
                } else if (value is string valueStr) {
                    targetField.SetValue(state, valueStr);
                } else {
                    _logger.LogError($"Type mismatch fieldType={fieldType} entityType={entityType}");
                }
            } else if (fieldType == typeof(DateTime)) {
                if (value is null) {
                    targetField.SetValue(state, MinEdmTime);
                } else if (value is DateTime valueDt) {
                    targetField.SetValue(state, valueDt);
                } else if (value is DateTimeOffset valueDto) {
                    targetField.SetValue(state, valueDto.UtcDateTime);
                } else {
                    _logger.LogError($"Type mismatch fieldType={fieldType} entityType={entityType}");
                }
            } else {
                _logger.LogError($"Unhandled field type={fieldType}");
            }
        }
    }

    public abstract Task ReadStateAsync<T>(string stateName, GrainId grainId, IGrainState<T> grainState);
    public abstract Task WriteStateAsync<T>(string stateName, GrainId grainId, IGrainState<T> grainState);
    public abstract Task ClearStateAsync<T>(string stateName, GrainId grainId, IGrainState<T> grainState);
}