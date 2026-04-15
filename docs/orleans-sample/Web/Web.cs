using System.Net;
using System.Net.Sockets;
using Microsoft.CodeAnalysis;
using Orleans.Providers.MongoDB.Configuration;
using Orleans.Configuration;
using Orleans.Hosting;
using My.Logic; // This ensures the grains assembly is loaded
using My.StorageProviders;

var builder = WebApplication.CreateBuilder(args);

// Add services to the container
builder.Services.AddControllers();
builder.Services.AddRazorPages();
builder.Services.AddEndpointsApiExplorer();
builder.Services.AddSwaggerGen();

// Configure Orleans
string mongodbConnection = Environment.GetEnvironmentVariable("MONGODB_CONNECTION") ?? "";
bool useMongoDBMembership = !string.IsNullOrEmpty(mongodbConnection);
bool useLocalhostClustering = !useMongoDBMembership;
string dataFolder = Environment.GetEnvironmentVariable("DATA_FOLDER") ?? "../tmp/data";

Console.WriteLine($"MongoDB Connection: {mongodbConnection}");
Console.WriteLine($"Using Localhost Clustering: {useLocalhostClustering}");
Console.WriteLine($"Using MongoDB Membership: {useMongoDBMembership}");
Console.WriteLine($"Data Folder: {dataFolder} = {Path.GetFullPath(dataFolder)}");

// This looks more "complicated" than it is, 
// but it's just choosing between two simple configurations
// 1. useLocalhostClustering: use an integrated local silo for debugging
// 2. useMongoDBMembership: use an Orleans Client to connect to an external cluster

if (useLocalhostClustering)
{
    // Integrated local silo for debugging
    builder.Host.UseOrleans(siloBuilder =>
    {
        siloBuilder.UseLocalhostClustering();

        siloBuilder.AddJsonFileStorage("JsonFileStorage", options =>
        {
            options.RootDirectory = dataFolder;
            options.FileExtension = ".json";
            options.TypeNameHandling = Newtonsoft.Json.TypeNameHandling.None;
        });
    });
}
else
{
    // Orleans Client to connect to external cluster via MongoDB Membership
    builder.Host.UseOrleansClient(clientBuilder =>
    {
        clientBuilder.UseMongoDBClient(mongodbConnection);
        clientBuilder.UseMongoDBClustering(options =>
        {
            options.DatabaseName = "orleans";
            options.Strategy = MongoDBMembershipStrategy.SingleDocument;
        });
    });
}

var app = builder.Build();

// if (app.Environment.IsDevelopment())
{
    app.UseSwagger();
    app.UseSwaggerUI();
}

// app.UseHttpsRedirection();
app.UseStaticFiles();
app.UseRouting();
app.UseAuthorization();

// Redirect root to Index page
app.MapGet("/", () => Results.Redirect("/Index"));

app.MapRazorPages();
app.MapControllers();

app.Run();