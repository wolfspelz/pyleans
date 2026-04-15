using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;
using Microsoft.Extensions.Options;
using Orleans.Configuration;
using Orleans.Providers;
using Orleans.Storage;

public class DummyGrainStorageOptionsValidator<GrainStorageOptionsType> : IConfigurationValidator
    where GrainStorageOptionsType : class
{
    public DummyGrainStorageOptionsValidator(GrainStorageOptionsType options, string name) { }
    public void ValidateConfiguration() { }
}

public static class GrainStorageFactory
{
    public static IGrainStorage Create<GrainStorageType,GrainStorageOptionsType>(IServiceProvider services, object? maybeName)
        where GrainStorageType : IGrainStorage
        where GrainStorageOptionsType : class
    {
        if (maybeName is not string name) {
            throw new ArgumentException("name must be a string");
        }
        var optionsMonitor = services.GetRequiredService<IOptionsMonitor<GrainStorageOptionsType>>();
        return ActivatorUtilities.CreateInstance<GrainStorageType>(services, name, optionsMonitor.Get(name)!);
    }
}

public static class GrainStorageSiloBuilderExtensions
{
    public static IServiceCollection AddGrainStorage<GrainStorageType,GrainStorageOptionsType>(
        this IServiceCollection services,
        string name,
        Action<GrainStorageOptionsType> configureOptions
    )
        where GrainStorageType : IGrainStorage
        where GrainStorageOptionsType : class
    {
        Action<OptionsBuilder<GrainStorageOptionsType>> configurer = ob => ob.Configure(configureOptions);
        configurer.Invoke(services.AddOptions<GrainStorageOptionsType>(name));
        services.AddTransient<IConfigurationValidator>(sp => new DummyGrainStorageOptionsValidator<GrainStorageOptionsType>(sp.GetRequiredService<IOptionsMonitor<GrainStorageOptionsType>>().Get(name), name));
        services.ConfigureNamedOptionForLogging<GrainStorageOptionsType>(name);
        services.TryAddSingleton<IGrainStorage>(sp => sp.GetRequiredKeyedService<IGrainStorage>(name));
        services.AddKeyedSingleton<IGrainStorage>(name, GrainStorageFactory.Create<GrainStorageType,GrainStorageOptionsType>);
        services.AddKeyedSingleton<ILifecycleParticipant<ISiloLifecycle>>(name, (s, n) => (ILifecycleParticipant<ISiloLifecycle>)s.GetRequiredKeyedService<IGrainStorage>(n));
        return services;
    }

    public static IServiceCollection AddGrainStorage<GrainStorageType,GrainStorageOptionsType>(
        this IServiceCollection services,
        string name,
        Action<OptionsBuilder<GrainStorageOptionsType>>? configureOptions = null
    )
        where GrainStorageType : IGrainStorage
        where GrainStorageOptionsType : class
    {
        configureOptions?.Invoke(services.AddOptions<GrainStorageOptionsType>(name));
        services.AddTransient<IConfigurationValidator>(sp => new DummyGrainStorageOptionsValidator<GrainStorageOptionsType>(sp.GetRequiredService<IOptionsMonitor<GrainStorageOptionsType>>().Get(name), name));
        services.ConfigureNamedOptionForLogging<GrainStorageOptionsType>(name);
        services.TryAddSingleton<IGrainStorage>(sp => sp.GetRequiredKeyedService<IGrainStorage>(name));
        services.AddKeyedSingleton<IGrainStorage>(name, GrainStorageFactory.Create<GrainStorageType,GrainStorageOptionsType>);
        services.AddKeyedSingleton<ILifecycleParticipant<ISiloLifecycle>>(name, (s, n) => (ILifecycleParticipant<ISiloLifecycle>)s.GetRequiredKeyedService<IGrainStorage>(n));
        return services;
    }
}