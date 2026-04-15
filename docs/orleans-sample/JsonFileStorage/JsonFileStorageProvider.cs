using System.Collections.Immutable;
using System.Text;
using Microsoft.Extensions.Logging;
using Newtonsoft.Json;
using Orleans.Serialization;

namespace My.StorageProviders;

public static class JsonFileStorage
{
    public const string StorageProviderName = "JsonFileStorage";
}

public class JsonFileStorageOptions
{
    public string RootDirectory = @".";
    public string FileExtension = ".json";

    public bool UseFullAssemblyNames = false;
    public bool IndentJson = true;
    public TypeNameHandling TypeNameHandling = Newtonsoft.Json.TypeNameHandling.All;
    public PreserveReferencesHandling PreserveReferencesHandling = Newtonsoft.Json.PreserveReferencesHandling.None;

    public int InitStage = DEFAULT_INIT_STAGE;
    public const int DEFAULT_INIT_STAGE = ServiceLifecycleStage.ApplicationServices;
}

public class JsonFileStorageProvider : GrainStorageBase
{
    private readonly string _name;
    private readonly JsonFileStorageOptions _options;
    private readonly JsonSerializerSettings _jsonSettings;

    public JsonFileStorageProvider(string name, JsonFileStorageOptions options, IServiceProvider services, ILoggerFactory loggerFactory): base(name, loggerFactory)
    {
        _name = name;
        _options = options;
        _jsonSettings = OrleansJsonSerializerSettings.UpdateSerializerSettings(
            OrleansJsonSerializerSettings.GetDefaultSerializerSettings(services)!,
            _options.UseFullAssemblyNames,
            _options.IndentJson,
            _options.TypeNameHandling
        )!;
        _jsonSettings.PreserveReferencesHandling = _options.PreserveReferencesHandling;
    }

    #region Interface

    public override async Task WriteStateAsync<T>(string grainType, GrainId grainId, IGrainState<T> grainState)
    {
        await Task.CompletedTask;
        string primaryKey = grainId.GetPrimaryKeyString();
        var filePath = GetFilePath(grainType, primaryKey, grainState);
        try {
            var data = JsonConvert.SerializeObject(grainState, _jsonSettings);
            File.WriteAllText(filePath, data, Encoding.UTF8);
        } catch (Exception ex) {
            _logger.LogError(0, $"Error writing: filePath={filePath} {ex.Message}");
            throw;
        }
    }

    public override async Task ReadStateAsync<T>(string grainType, GrainId grainId, IGrainState<T> grainState)
    {
        await Task.CompletedTask;
        string primaryKey = grainId.GetPrimaryKeyString();
        var filePath = GetFilePath(grainType, primaryKey, grainState);
        try {
            var fileInfo = new FileInfo(filePath);
            if (fileInfo.Exists) {
                var data = File.ReadAllText(filePath, Encoding.UTF8);
                var result = JsonConvert.DeserializeObject(data, grainState.GetType(), _jsonSettings);
                grainState.State = (result as IGrainState<T>)!.State;
            }
        } catch (Exception ex) {
            _logger.LogError(0, $"Error reading: filePath={filePath} {ex.Message}");
            throw;
        }
    }

    public override Task ClearStateAsync<T>(string grainType, GrainId grainId, IGrainState<T> grainState)
    {
        string primaryKey = grainId.GetPrimaryKeyString();
        var filePath = GetFilePath(grainType, primaryKey, grainState);
        try {
            var fileInfo = new FileInfo(filePath);
            if (fileInfo.Exists) {
                fileInfo.Delete();
            }
        } catch (Exception ex) {
            _logger.LogError(0, $"Error deleting: filePath={filePath} {ex.Message}");
            throw;
        }
        return Task.CompletedTask;
    }

    #endregion

    #region Internal

    static readonly ImmutableArray<char> InvalidFileNameChars = [..Path.GetInvalidFileNameChars().Append('%')];

    private string FilesystemSafeName(string name)
    {
        var safeName = new StringBuilder();
        foreach (var c in name) {
            if (InvalidFileNameChars.Contains(c)) {
                safeName.Append('%');
                safeName.Append(Convert.ToInt32(c));
            } else {
                safeName.Append(c);
            }
        }
        return safeName.ToString();
    }

    protected string GetFilePath<T>(string grainType, string primaryKey, IGrainState<T> grainState)
    {
        if (!Directory.Exists(_options.RootDirectory)) {
            throw new Exception($"{nameof(JsonFileStorage)}: root directory does not exist: {_options.RootDirectory} pwd={Directory.GetCurrentDirectory()}");
        }
        var fileName = FilesystemSafeName(grainType) + "-" + FilesystemSafeName(primaryKey) + _options.FileExtension;
        var filePath = Path.Combine(_options.RootDirectory, fileName);
        return filePath;
    }

    #endregion
}

#region Provider registration

public static class JsonFileStorageSiloBuilderExtensions
{
    public static ISiloBuilder AddJsonFileStorage(this ISiloBuilder builder, string name, Action<JsonFileStorageOptions> configureOptions)
    {
        builder.ConfigureServices(services => {
            services.AddGrainStorage<JsonFileStorageProvider,JsonFileStorageOptions>(name, configureOptions);
        });
        return builder;
    }
}

#endregion
